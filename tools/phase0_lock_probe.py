#!/usr/bin/env python3
"""Read-only Trnje flock probe. Never starts/stops a worker or removes a lock file."""
from __future__ import annotations
import fcntl, hashlib, json, os, stat, subprocess, time
from pathlib import Path

HOME=Path.home()
STATE=HOME/".local/state/trnje-worker"
LOCK=STATE/"worker.lock"
STATE_FILE=STATE/"state.json"
PUBLISHER=HOME/"bin/trnje-publish"

def proc_details(pid):
    p=Path("/proc")/str(pid)
    try:
        comm=(p/"comm").read_text(errors="replace").strip()
        raw=(p/"cmdline").read_bytes().split(b"\0")
        argv=[Path(os.fsdecode(x)).name if (b"/" in x) else os.fsdecode(x) for x in raw if x]
        stat_fields=(p/"stat").read_text().split()
        return {"pid":pid,"ppid":int(stat_fields[3]),"state":stat_fields[2],
                "comm":comm,"argv_basename":argv[:6]}
    except Exception as e:
        return {"pid":pid,"status":"BLOCKED","error":type(e).__name__}

def proc_locks_match(st):
    try:
        key=f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
        matches=[]
        for line in Path("/proc/locks").read_text().splitlines():
            parts=line.split()
            if len(parts)>5 and parts[5]==key:
                matches.append({"raw":line.strip(),"pid":int(parts[4]) if parts[4].isdigit() else None})
        return {"status":"PASS","matches":matches}
    except Exception as e:
        return {"status":"BLOCKED","error":type(e).__name__}

def fd_holders(st):
    found=[]
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit() or int(p.name)==os.getpid():
                continue
            fd_dir=p/"fd"
            try:
                for fd in fd_dir.iterdir():
                    try:
                        x=fd.stat()
                        if x.st_dev==st.st_dev and x.st_ino==st.st_ino:
                            found.append(proc_details(int(p.name)))
                            break
                    except OSError:
                        pass
            except OSError:
                pass
        return {"status":"PASS","processes":found}
    except Exception as e:
        return {"status":"BLOCKED","error":type(e).__name__,"processes":found}

def state():
    try:
        raw=STATE_FILE.read_bytes()
        obj=json.loads(raw)
        tasks=obj.get("tasks",{}) if isinstance(obj,dict) else {}
        counts={}
        if isinstance(tasks,dict):
            for item in tasks.values():
                k=item.get("status","UNKNOWN") if isinstance(item,dict) else "INVALID"
                counts[k]=counts.get(k,0)+1
        return {"status":"PASS","sha256":hashlib.sha256(raw).hexdigest(),
                "last_seen":obj.get("last_seen") if isinstance(obj,dict) else None,
                "task_count":len(tasks) if isinstance(tasks,dict) else None,
                "task_status_counts":counts}
    except FileNotFoundError:
        return {"status":"ABSENT"}
    except Exception as e:
        return {"status":"BLOCKED","error":type(e).__name__}

def main():
    report={"title":"Trnje worker lock probe","created_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
            "scope":"Read-only probe. Does not unlink/truncate/write the lock file, signal processes, start/stop workers, or modify Git/queue/state."}
    if not LOCK.exists():
        report["lock"]={"status":"ABSENT","probe":"NOT_RUN"}
    else:
        fd=None
        try:
            fd=os.open(LOCK,os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0))
            st=os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeError("lock path is not a regular file")
            info={"path":str(LOCK),"device":st.st_dev,"inode":st.st_ino,"size":st.st_size,
                  "mode":oct(stat.S_IMODE(st.st_mode))}
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                info["probe"]="UNLOCKED_AT_PROBE"
                fcntl.flock(fd,fcntl.LOCK_UN)
            except BlockingIOError:
                info["probe"]="LOCKED_AT_PROBE"
            except OSError as e:
                info["probe"]="BLOCKED"
                info["probe_error"]=type(e).__name__
            info["proc_locks"]=proc_locks_match(st)
            info["open_fd_processes"]=fd_holders(st)
            report["lock"]=info
        except Exception as e:
            report["lock"]={"status":"BLOCKED","error":type(e).__name__}
        finally:
            if fd is not None:
                os.close(fd)
    matches=[]
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit() or int(p.name)==os.getpid():
                continue
            d=proc_details(int(p.name))
            args=" ".join(d.get("argv_basename",[])).lower()
            comm=d.get("comm","").lower()
            if "trnje-worker" in args or "llama" in args or "trnje_termux_worker" in args:
                matches.append(d)
        report["worker_processes"]={"status":"PASS","matches":matches}
    except Exception as e:
        report["worker_processes"]={"status":"BLOCKED","error":type(e).__name__}
    report["worker_state"]=state()
    report["self"]={"pid":os.getpid(),"parent_pid":os.getppid()}
    payload=json.dumps(report,indent=2,sort_keys=True)
    outdir=STATE/"snapshots"
    outdir.mkdir(parents=True,exist_ok=True,mode=0o700)
    out=outdir/f"lock-probe-{time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())}.json"
    fd=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as f:
        f.write(payload+"\n")
    print("LOCK_PROBE=PASS; report="+str(out))
    if not PUBLISHER.is_file() or not os.access(PUBLISHER,os.X_OK):
        print("PUBLISH=BLOCKED; publisher unavailable")
        return 2
    p=subprocess.run([str(PUBLISHER),str(out)],cwd=str(HOME/"trnje-e2e-aRhnEF"/"repo"),
                     stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                     timeout=120,check=False,shell=False,
                     env={**os.environ,"GIT_PAGER":"cat","PAGER":"cat","GIT_TERMINAL_PROMPT":"0"})
    print("PUBLISH="+("PASS" if p.returncode==0 else "FAIL"))
    if p.returncode:
        print(p.stderr.decode("utf-8","replace")[:1000])
        return 2
    print(p.stdout.decode("utf-8","replace")[:3000])
    return 0

if __name__=="__main__":
    raise SystemExit(main())
