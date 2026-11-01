#!/usr/bin/env python3
"""Read-only Trnje Phase 0 snapshot; writes only its report outside the repo."""
from __future__ import annotations
import hashlib, json, os, platform, shutil, socket, stat, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
REPO = HOME / "trnje-e2e-aRhnEF" / "repo"
STATE = HOME / ".local/state/trnje-worker"
STATE_FILE = STATE / "state.json"
LOCK_FILE = STATE / "worker.lock"
PUBLISHER = HOME / "bin/trnje-publish"
MAX_FILE = 128 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024

def run(argv, cwd=None, timeout=20):
    try:
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                           check=False, shell=False, env={**os.environ, "GIT_PAGER":"cat",
                           "PAGER":"cat", "GIT_TERMINAL_PROMPT":"0"})
        return {"status":"PASS" if p.returncode == 0 else "FAIL", "exit":p.returncode,
                "out":p.stdout, "err":p.stderr[:1000]}
    except Exception as e:
        return {"status":"BLOCKED", "exit":None, "out":b"", "err":type(e).__name__}

def text_result(x):
    return {"status":x["status"], "exit":x["exit"],
            "output":x["out"].decode("utf-8","replace")[:12000],
            "error":x["err"].decode("utf-8","replace")[:1000] if isinstance(x["err"],bytes) else x["err"]}

def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()

def safe_file_hash(p, total):
    try:
        st=p.lstat()
        if stat.S_ISLNK(st.st_mode):
            target=os.readlink(p)
            return {"type":"symlink","target":target,"sha256":sha_bytes(os.fsencode(target))}
        if not stat.S_ISREG(st.st_mode):
            return {"type":"other","mode":oct(st.st_mode)}
        if st.st_size > MAX_FILE or total[0]+st.st_size > MAX_TOTAL:
            return {"type":"file","size":st.st_size,"status":"BLOCKED_SIZE_LIMIT"}
        h=hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda:f.read(1024*1024),b""):
                h.update(chunk)
                total[0]+=len(chunk)
        return {"type":"file","size":st.st_size,"mode":oct(stat.S_IMODE(st.st_mode)),"sha256":h.hexdigest()}
    except Exception as e:
        return {"status":"BLOCKED","error":type(e).__name__}

def proc_snapshot():
    out=[]
    try:
        entries=list(Path('/proc').iterdir())
    except OSError:
        return {"status":"BLOCKED","reason":"/proc unavailable"}
    for p in entries:
        if not p.name.isdigit():
            continue
        try:
            comm=(p/'comm').read_text(errors='replace').strip()
            cmd=(p/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
            low=(comm+' '+cmd).lower()
            if any(k in low for k in ('trnje','termux-worker','llama','emberlane')):
                fields=(p/'stat').read_text().split()
                out.append({"pid":int(p.name),"ppid":int(fields[3]),"state":fields[2],
                            "comm":comm,"cmdline_basename":Path(cmd.split(' ',1)[0]).name if cmd else comm})
        except (OSError,ValueError,IndexError):
            pass
    return {"status":"PASS","matching_processes":out}

def lock_snapshot():
    if not LOCK_FILE.exists():
        return {"status":"ABSENT","path":str(LOCK_FILE)}
    try:
        fd=os.open(LOCK_FILE,os.O_RDONLY|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NOFOLLOW',0))
        try:
            st=os.fstat(fd)
            result={"status":"PRESENT","path":str(LOCK_FILE),"device":st.st_dev,"inode":st.st_ino,
                    "size":st.st_size,"mtime_ns":st.st_mtime_ns,"mode":oct(stat.S_IMODE(st.st_mode)),
                    "flock_owner":"UNKNOWN"}
            try:
                matches=[]
                for line in Path('/proc/locks').read_text().splitlines():
                    if f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}" in line:
                        matches.append(line.strip())
                result["proc_locks_matches"]=matches
                if matches:
                    result["flock_owner"]="PRESENT_IN_PROC_LOCKS"
                elif Path('/proc/locks').exists():
                    result["flock_owner"]="NO_MATCH_OBSERVED"
            except OSError:
                result["proc_locks_status"]="BLOCKED"
            return result
        finally:
            os.close(fd)
    except Exception as e:
        return {"status":"BLOCKED","error":type(e).__name__}

def state_summary():
    if not STATE_FILE.exists():
        return {"status":"ABSENT","path":str(STATE_FILE)}
    try:
        raw=STATE_FILE.read_bytes()
        obj=json.loads(raw)
        tasks=obj.get('tasks',{}) if isinstance(obj,dict) else {}
        counts={}
        if isinstance(tasks,dict):
            for v in tasks.values():
                s=v.get('status','UNKNOWN') if isinstance(v,dict) else 'INVALID'
                counts[s]=counts.get(s,0)+1
        return {"status":"PASS","path":str(STATE_FILE),"size":len(raw),"sha256":sha_bytes(raw),
                "top_level_keys":sorted(obj.keys()) if isinstance(obj,dict) else [],
                "version":obj.get('version') if isinstance(obj,dict) else None,
                "last_seen":obj.get('last_seen') if isinstance(obj,dict) else None,
                "task_count":len(tasks) if isinstance(tasks,dict) else None,
                "task_status_counts":counts}
    except Exception as e:
        return {"status":"BLOCKED","error":type(e).__name__}

def resources():
    r={"hostname":socket.gethostname(),"platform":platform.platform(),"python":sys.version.split()[0]}
    try:
        mem={}
        for line in Path('/proc/meminfo').read_text().splitlines():
            k,_,v=line.partition(':')
            if k in ('MemTotal','MemAvailable','SwapTotal','SwapFree'):
                mem[k]=v.strip()
        r["memory"]=mem
    except OSError:
        r["memory"]={"status":"BLOCKED"}
    for label,path in [('home',HOME),('repo',REPO)]:
        try:
            d=shutil.disk_usage(path)
            r[label+"_disk"]={"total":d.total,"used":d.used,"free":d.free}
        except OSError as e:
            r[label+"_disk"]={"status":"BLOCKED","error":type(e).__name__}
    zones=[]
    try:
        for z in Path('/sys/class/thermal').glob('thermal_zone*'):
            try:
                typ=(z/'type').read_text().strip()
                val=int((z/'temp').read_text().strip())
                zones.append({"type":typ,"temperature_c":val/1000 if abs(val)>1000 else val})
            except (OSError,ValueError):
                pass
    except OSError:
        pass
    r["thermal"]={"status":"PASS" if zones else "UNKNOWN","zones":zones[:20]}
    return r

def git_snapshot():
    top=run(['git','rev-parse','--show-toplevel'],REPO)
    if top["status"]!="PASS":
        return {"status":"BLOCKED","reason":"repo root unavailable"}
    root=Path(top["out"].decode().strip()).resolve()
    if root!=REPO.resolve():
        return {"status":"BLOCKED","reason":"unexpected git root"}
    data={"status":"PASS","root":str(root)}
    for name,cmd in [('branch',['git','branch','--show-current']),
                     ('head',['git','rev-parse','HEAD']),
                     ('status',['git','status','--short','--untracked-files=all']),
                     ('diff_stat',['git','diff','--stat','HEAD']),
                     ('diff_check',['git','diff','--check','HEAD'])]:
        data[name]=text_result(run(cmd,root))
    for name,cmd in [('worktree_diff_sha256',['git','diff','--binary','HEAD']),
                     ('cached_diff_sha256',['git','diff','--cached','--binary'])]:
        x=run(cmd,root,60)
        data[name]={"status":x["status"],"sha256":sha_bytes(x["out"]) if x["status"]=="PASS" else None,
                    "bytes":len(x["out"])}
    tracked=run(['git','diff','--name-only','-z','HEAD'],root)
    untracked=run(['git','ls-files','--others','--exclude-standard','-z'],root)
    if tracked["status"]!="PASS" or untracked["status"]!="PASS":
        return {**data,"manifest_status":"BLOCKED"}
    names=set(os.fsdecode(x) for x in tracked["out"].split(b'\0') if x)
    names.update(os.fsdecode(x) for x in untracked["out"].split(b'\0') if x)
    total=[0]
    manifest=[]
    for name in sorted(names):
        manifest.append({"path":name,**safe_file_hash(root/name,total)})
    data["manifest"]={"status":"PASS","file_count":len(manifest),"bytes_hashed":total[0],
                      "sha256":sha_bytes(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()),
                      "entries":manifest}
    return data

def main():
    start=time.time()
    tmux=run(['tmux','list-sessions','-F','#{session_name} #{session_id} #{session_windows}'],timeout=10) if shutil.which('tmux') else {"status":"ABSENT","exit":None,"out":b"","err":"tmux not installed"}
    report={"title":"Trnje Phase 0 read-only snapshot",
            "created_utc":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
            "scope":"Read-only inspection. No process signals, worker start/stop, git writes, queue edits, or lock acquisition. Report publication writes only the report outside the checkout.",
            "repo":git_snapshot(),"processes":proc_snapshot(),"tmux_sessions":text_result(tmux),
            "lock":lock_snapshot(),"worker_state":state_summary(),"resources":resources()}
    worker=HOME/'bin/trnje-worker'
    report["installed_worker"]={"path":str(worker),**safe_file_hash(worker,[0])} if worker.is_file() else {"status":"ABSENT","path":str(worker)}
    report["duration_seconds"]=round(time.time()-start,3)
    payload=json.dumps(report,indent=2,sort_keys=True,ensure_ascii=False)
    print(payload)
    outdir=HOME/'.local/state/trnje-worker/snapshots'
    outdir.mkdir(parents=True,exist_ok=True,mode=0o700)
    out=outdir/f"phase0-{time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())}.json"
    fd=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:
        f.write(payload+'\n')
    if not PUBLISHER.is_file() or not os.access(PUBLISHER,os.X_OK):
        print(f"PUBLISH=BLOCKED; report={out}",file=sys.stderr)
        return 2
    pub=run([str(PUBLISHER),str(out)],REPO,120)
    print("PUBLISH="+pub["status"],file=sys.stderr)
    if pub["status"]=="PASS":
        print(pub["out"].decode('utf-8','replace')[:4000],file=sys.stderr)
        return 0
    print("REPORT="+str(out),file=sys.stderr)
    return 2

if __name__=="__main__":
    raise SystemExit(main())
