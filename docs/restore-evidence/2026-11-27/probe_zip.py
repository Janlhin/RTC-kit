import json, zipfile, sys
zip_path = sys.argv[1]
with zipfile.ZipFile(zip_path) as z:
    names = z.namelist()
    print("ENTRIES=" + ",".join(names))
    a = json.loads(z.read("state/alarms.json").decode("utf-8"))
    r = json.loads(z.read("rules/routines.json").decode("utf-8"))
    print("planDate=" + str(a.get("planDate")))
    print("rev=" + str(a.get("rev")))
    print("windows=" + str(len(a.get("windows") or [])))
    print("alarmIds=" + ",".join(w.get("id","") for w in (a.get("windows") or [])))
    print("routineIds=" + ",".join(w.get("id","") for w in (r.get("alarmWindows") or [])))
