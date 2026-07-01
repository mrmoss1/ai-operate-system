#!/usr/bin/env python3
"""agent_fleet_lint.py -- co-location + six-folder conformance scanner for the agent fleet.

Extends Agent Fleet Triage with structural conformance (added 2026-06-21, t0109). Walks every
agent package in the tree and flags, per agent:
  * SIX-FOLDER drift  -- a component delivered as a flat NN-name.html instead of NN-name/index.html,
                         or a missing component folder.
  * LOOSE-ROOT files  -- engine .py / data / json sitting at the agent ROOT instead of co-located
                         into 03-process/ (executables) or 04-context/ (inputs/data).
  * STRAY junk        -- __pycache__/, *.pyc, .DS_Store anywhere in the package (never ships).

Read-only. Prints a per-agent findings table + summary; exit 1 if any non-clean agent is found.
Run: python3 agent_fleet_lint.py [operate_root]
"""
import os, sys, glob

COMPONENTS = ["01-start-here","02-golden-example","03-process","04-context","05-quality","06-kpis"]
ROOT_ALLOW = {"README.md","README.html",".gitignore"}   # tolerated at agent root
JUNK_NAMES = {"__pycache__",".DS_Store"}

def _operate_root(start=None):
    # An explicit target must be honored: if it (or an ancestor) carries the
    # '0. Chief of Staff/atomic_write.py' marker, use the marked root; otherwise
    # scan the given path AS-IS with a warning -- never silently fall back to CWD.
    # (The silent CWD fallback scanned \Operate instead of a tenant tree -- t0617.)
    explicit = start is not None
    base = os.path.abspath(start if start else __file__)
    p = base
    for _ in range(12):
        if os.path.isfile(os.path.join(p,"0. Chief of Staff","atomic_write.py")):
            return p
        parent = os.path.dirname(p)
        if parent == p: break
        p = parent
    if explicit:
        if os.path.isdir(base):
            sys.stderr.write("[agent_fleet_lint] WARNING: no '0. Chief of Staff/atomic_write.py' "
                             "marker at or above %s; scanning it as given (not CWD).\n" % base)
            return base
        sys.stderr.write("[agent_fleet_lint] ERROR: %s is not a directory.\n" % base)
        sys.exit(2)
    sys.stderr.write("[agent_fleet_lint] WARNING: no Operate marker found from script "
                     "location; falling back to CWD %s.\n" % os.getcwd())
    return os.getcwd()

EXCLUDE_SEGMENTS=("_archive","/archive/","6. Shipped","/runs/",".backup")
def _excluded(path):
    p=path.replace(os.sep,"/")
    return any(seg.strip("/") in p.split("/") or seg in p for seg in EXCLUDE_SEGMENTS)

def find_agent_dirs(root):
    """An agent package = a dir UNDER an Agents/agents/ path holding >=2 NN component markers."""
    out=set()
    for dirpath,dirs,files in os.walk(root):
        dirs[:]=[d for d in dirs if d!="__pycache__" and not d.endswith(".backup")]
        p=dirpath.replace(os.sep,"/")
        if not ("/Agents/" in p+"/" or "/agents/" in p+"/"): continue
        if _excluded(p): continue
        names=set(dirs)|set(files)
        if sum(1 for c in COMPONENTS if c in names or (c+".html") in names)>=2:
            out.add(dirpath)
    return sorted(d for d in out if os.path.basename(d) not in COMPONENTS)

def scan_agent(d):
    flat=[]; missing=[]; loose=[]; junk=[]
    entries=os.listdir(d)
    for c in COMPONENTS:
        has_folder=os.path.isfile(os.path.join(d,c,"index.html"))
        has_flat=os.path.isfile(os.path.join(d,c+".html"))
        if has_folder: pass
        elif has_flat: flat.append(c+".html")
        else: missing.append(c)
    # loose root files: regular files at agent root not part of the 6 flat components, not allowlisted
    flat_comp_names={c+".html" for c in COMPONENTS}
    for e in entries:
        full=os.path.join(d,e)
        if os.path.isfile(full):
            if e in flat_comp_names: continue          # counted as flat-file drift already
            if e in ROOT_ALLOW: continue
            if e.startswith(".fuse_hidden"): continue  # FUSE mount cruft, not agent content
            if ".backup-" in e or e.endswith(".backup"): junk.append(e); continue  # sweepable backups
            if e==".DS_Store" or e.endswith(".pyc"): junk.append(e); continue
            loose.append(e)
    # stray junk anywhere in the package
    for dp,dirs,files in os.walk(d):
        for nm in list(dirs)+files:
            if nm=="__pycache__" or nm==".DS_Store" or nm.endswith(".pyc"):
                rel=os.path.relpath(os.path.join(dp,nm),d)
                if rel not in junk: junk.append(rel)
    return flat,missing,loose,junk

def main():
    root=_operate_root(sys.argv[1] if len(sys.argv)>1 else None)
    agents=find_agent_dirs(root)
    rows=[]; nonclean=0
    for d in agents:
        flat,missing,loose,junk=scan_agent(d)
        verdict="CLEAN"
        if flat or missing: verdict="NON-SIX-FOLDER"
        elif loose or junk: verdict="NEEDS-COLOCATION"
        if verdict!="CLEAN": nonclean+=1
        rows.append((os.path.relpath(d,root),verdict,flat,missing,loose,junk))
    print(f"Agent Fleet co-location + six-folder scan -- {len(agents)} packages, {nonclean} non-clean\n")
    for rel,v,flat,missing,loose,junk in rows:
        if v=="CLEAN": continue
        print(f"[{v}] {rel}")
        if flat:    print(f"    flat-file components : {flat}")
        if missing: print(f"    missing components   : {missing}")
        if loose:   print(f"    loose root files     : {loose}")
        if junk:    print(f"    stray junk           : {junk}")
    clean=len(agents)-nonclean
    print(f"\nSummary: {clean} CLEAN | {nonclean} need work "
          f"({sum(1 for r in rows if r[1]=='NON-SIX-FOLDER')} non-six-folder, "
          f"{sum(1 for r in rows if r[1]=='NEEDS-COLOCATION')} colocation-only)")
    return 1 if nonclean else 0

if __name__=="__main__":
    sys.exit(main())
