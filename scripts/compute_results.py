#!/usr/bin/env python3
"""Build results.json (proximate results per sample) from data.json and names.json.

Each test has W1 = empty crucible, W2 = crucible + sample, W3 = crucible + residue.
Sample mass m = W2 - W1. Same formulas as index.html:

  Moisture %        = (W2 - W3) / m * 100            on the MC test
  Total moisture %  = (W2 - W3) / m * 100            on the TM test (only when tm is on)
  VM furnace loss % = (W2 - W3) / m * 100            on the VM test (as-received sample)
  VM (ar) %         = VM loss - moisture
  VM (db) %         = VM (ar) / (100 - moisture) * 100
  Ash (ar) %        = (W3 - W1) / m * 100            on the ASH test
  Ash (db) %        = Ash (ar) / (100 - moisture) * 100
  FC (ar) %         = 100 - moisture - VM (ar) - Ash (ar)
  FC (db) %         = 100 - VM (db) - Ash (db)

A sample is flagged with the same rules the weighing app shows in red: moisture above 17.5 %,
VM (db) above 17.5 %, Ash (db) above 12 %, total moisture above 60 % (only when tm is on),
VM (ar) below 5 %, the others (as received) below 1.5 %, or negative (not possible). Flagged samples go into
results.json only after they are approved in the weighing app; until then they are listed
under "awaiting_approval". An approval stops counting if the sample's weighings change.

retest.json lists the same not-approved flagged samples on their own, with the tests to
redo, so the lab knows what to weigh again.

corrections.csv is corrections.json (every weight the app overwrote or reset) as a table, one row per weight, newest first.

results.json, results.csv, retest.json, retest.csv and corrections.csv are only rewritten when something changes, so the workflow commits only then.
"""
import csv
import io
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))


def load(name, default):
    if not (ROOT / name).exists() and name == "approvals.json":
        return default  # created by the app on the first approval
    try:
        return json.loads((ROOT / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"warning: could not read {name}: {e}", file=sys.stderr)
        return default


def norm_code(c):
    return re.sub(r"[\s-]", "", str(c or "")).upper()


def complete(w):
    return bool(w) and len(w) >= 3 and all(v is not None for v in w[:3])


def mass(w):
    return w[1] - w[0] if w and len(w) >= 2 and w[0] is not None and w[1] is not None else None


def loss_pct(w):
    if not complete(w):
        return None
    m = w[1] - w[0]
    return (w[1] - w[2]) / m * 100 if m > 0 else None


def residue_pct(w):
    if not complete(w):
        return None
    m = w[1] - w[0]
    return (w[2] - w[0]) / m * 100 if m > 0 else None


TESTS = ("MC", "VM", "ASH", "TM")


def w_key(w):
    return [list((w or {}).get(k) or [])[:3] for k in TESTS]


def r2(v):
    return None if v is None else round(v, 2)


# [lower, upper] %, None = no limit. The supervisor sets them (weighing app Setup or the results page) in
# data.json settings.limits; these are the defaults. Keep in step with DEF_LIMITS in weighing-beta index.html / results.html.
DEF_LIMITS = {"MC": [1.5, 17.5], "VM": [5, 17.5], "ASH": [1.5, 12], "FC": [None, None], "TM": [1.5, 60]}
DB_LIMITS = ("VM", "ASH", "FC")  # compared on dry basis; MC and TM as received
FLAG_ORDER = ["MC", "VM", "ASH", "FC", "TM"]


def compute(code, s, names, limits):
    w = s.get("w") or {}
    mc, vml, ash_ar = loss_pct(w.get("MC")), loss_pct(w.get("VM")), residue_pct(w.get("ASH"))
    tm = loss_pct(w.get("TM")) if s.get("tm") else None
    vm_ar = vm_db = ash_db = fc_ar = fc_db = None
    if vml is not None and mc is not None:
        vm_ar = vml - mc
        if mc < 100:
            vm_db = vm_ar / (100 - mc) * 100
    if ash_ar is not None and mc is not None and mc < 100:
        ash_db = ash_ar / (100 - mc) * 100
    if None not in (mc, vm_ar, ash_ar):
        fc_ar = 100 - mc - vm_ar - ash_ar
    if None not in (vm_db, ash_db):
        fc_db = 100 - vm_db - ash_db

    done = {"MC": mc is not None, "VM": vml is not None, "ASH": ash_ar is not None}
    if s.get("tm"):
        done["TM"] = tm is not None
    any_w = any(v is not None for k in ("MC", "VM", "ASH", "TM") for v in (w.get(k) or []))
    status = "complete" if all(done.values()) else "incomplete" if any_w else "not started"

    checks = []
    for k in ("MC", "VM", "ASH"):
        m = mass(w.get(k))
        if m is not None and not 0.95 <= m <= 1.10:
            checks.append(f"{k} mass {m:.3f} g")
    if vm_ar is not None and vm_ar < 0:
        checks.append("VM negative")
    if mc is not None and mc > 50:
        checks.append("moisture > 50 %")
    if ash_ar is not None and ash_ar < 0:
        checks.append("ash negative")

    # Same numbers as the app: moisture and ash rounded to 2 dp, VM = loss - rounded moisture
    app_pct = {"MC": r2(mc), "VM": r2(vml - r2(mc)) if None not in (vml, mc) else None, "ASH": r2(ash_ar), "TM": r2(tm)}
    # Dry basis as the app works it out: rounded as-received value / (100 - rounded moisture)
    app_db = {k: r2(app_pct[k] / (100 - app_pct["MC"]) * 100)
              if None not in (app_pct[k], app_pct["MC"]) and app_pct["MC"] < 100 else None for k in ("VM", "ASH")}
    cmp = {"MC": app_pct["MC"], "VM": app_db["VM"], "ASH": app_db["ASH"], "TM": app_pct["TM"] if s.get("tm") else None,
           "FC": r2(100 - app_db["VM"] - app_db["ASH"]) if None not in (app_db["VM"], app_db["ASH"]) else None}
    flags = []
    for k in FLAG_ORDER:
        lo, hi = (limits.get(k) or [None, None])[:2]
        v, db = cmp.get(k), " db" if k in DB_LIMITS else ""
        if k != "FC" and app_pct.get(k) is not None and app_pct[k] < 0:
            flags.append(f"{k} {app_pct[k]:.2f} % negative")
        elif v is None:
            continue
        elif hi is not None and v > hi:
            flags.append(f"{k} {v:.2f} %{db} above {hi:g} %{db}")
        elif lo is not None and v < lo:
            flags.append(f"{k} {v:.2f} %{db} below {lo:g} %{db}")

    info = names.get(norm_code(code)) or {}
    return {
        "code": code,
        "name": info.get("name", ""),
        "supplier": info.get("supplier", ""),
        "date": info.get("date", ""),
        "status": status,
        "pending": [k for k, ok in done.items() if not ok],
        "moisture": r2(mc),
        "total_moisture": r2(tm),
        "vm_loss": r2(vml),
        "vm_ar": r2(vm_ar),
        "vm_db": r2(vm_db),
        "ash_ar": r2(ash_ar),
        "ash_db": r2(ash_db),
        "fc_ar": r2(fc_ar),
        "fc_db": r2(fc_db),
        "flags": flags,
        "checks": checks,
        "last_entry": s.get("last", ""),
        "_w": w,
    }


def code_key(c):
    m = re.match(r"^([A-Z]*)(\d+)(.*)$", c)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (c, 0, "")


def main():
    data = load("data.json", {})
    names_file = load("names.json", {})
    names = {norm_code(k): v for k, v in (names_file.get("names") or {}).items()}
    samples = data.get("samples") or {}
    appr_file = load("approvals.json", {})
    approvals = appr_file.get("approvals") or {}
    limits = {**DEF_LIMITS, **((data.get("settings") or {}).get("limits") or {})}

    results, awaiting = [], []
    for code, s in sorted(samples.items(), key=lambda kv: code_key(kv[0])):
        r = compute(code, s, names, limits)
        w = r.pop("_w")
        a = approvals.get(code)
        stale = bool(a and "w" in a and w_key(a["w"]) != w_key(w))
        if stale:
            a = None  # weighings changed since approval
        if r["flags"]:
            if not a:
                awaiting.append({k: r[k] for k in ("code", "name", "supplier", "date", "status", "flags", "checks")}
                                | {"retest": sorted({f.split(" ")[0] for f in r["flags"]}, key=FLAG_ORDER.index),
                                   "moisture": r["moisture"], "vm_ar": r["vm_ar"], "ash_ar": r["ash_ar"], "total_moisture": r["total_moisture"],
                                   "approval_outdated": stale})
                continue
            r["approved_by"], r["approved_at"] = a.get("by", ""), a.get("at", "")
        results.append(r)
    counts = {k: sum(r["status"] == k for r in results) for k in ("complete", "incomplete", "not started")}
    counts["awaiting approval"] = len(awaiting)

    out = {
        "app": "proximate-results",
        "version": 1,
        "generated": datetime.now(IST).isoformat(timespec="seconds"),
        "names_updated": names_file.get("updated", ""),
        "limits": limits,
        "dry_basis_limits": list(DB_LIMITS),
        "basis": {
            "moisture": "as received, (W2-W3)/m*100 on MC test",
            "total_moisture": "as received, (W2-W3)/m*100 on TM test, only for samples with tm on",
            "vm_ar": "VM furnace loss minus moisture",
            "vm_db": "vm_ar / (100 - moisture) * 100",
            "ash_ar": "(W3-W1)/m*100 on ASH test",
            "ash_db": "ash_ar / (100 - moisture) * 100",
            "fc": "100 minus the others, by difference",
        },
        "count": len(results),
        "counts": counts,
        "results": results,
        "awaiting_approval": [{k: v for k, v in x.items() if k in ("code", "name", "supplier", "date", "status", "flags", "checks")}
                              for x in awaiting],
    }
    retest = {
        "app": "proximate-retest",
        "version": 1,
        "generated": out["generated"],
        "limits": limits,
        "dry_basis_limits": list(DB_LIMITS),
        "rule": "moisture, VM (db), ash (db), fixed carbon (db) or total moisture above the upper limit, below the lower limit, or negative, and not approved",
        "count": len(awaiting),
        "by_test": {k: sum(k in x["retest"] for x in awaiting) for k in FLAG_ORDER},
        "samples": awaiting,
    }
    write_if_changed("results.json", out, f"{len(results)} samples, {counts}")
    write_if_changed("retest.json", retest, f"{len(awaiting)} samples to retest")
    write_results_csv(results)
    write_retest_csv(awaiting)
    write_corrections_csv(load("corrections.json", {}).get("entries") or [], names)


TEST_NAMES = {"MC": "Moisture", "VM": "VM", "ASH": "Ash", "TM": "Total moisture", "FC": "Fixed carbon (check VM and Ash)"}


def blank(v):
    return "" if v is None else v


def write_results_csv(results):
    """results.csv: the same rows as results.json, one row per sample, for Excel."""
    rows = [[r["code"], r["name"], r["supplier"], r["date"], r["status"], ", ".join(TEST_NAMES[k] for k in r["pending"]),
             blank(r["moisture"]), blank(r["total_moisture"]), blank(r["vm_loss"]), blank(r["vm_ar"]), blank(r["vm_db"]),
             blank(r["ash_ar"]), blank(r["ash_db"]), blank(r["fc_ar"]), blank(r["fc_db"]), "; ".join(r["flags"]),
             "; ".join(r["checks"]), r.get("approved_by", ""), r.get("approved_at", ""), r["last_entry"]]
            for r in results]
    write_csv("results.csv", ["Code", "Sample name", "Supplier", "Date", "Status", "Pending", "Moisture %",
                              "Total moisture %", "VM loss %", "VM (ar) %", "VM (db) %", "Ash (ar) %", "Ash (db) %",
                              "FC (ar) %", "FC (db) %", "Flags", "Checks", "Approved by", "Approved at", "Last entry"], rows)


def write_retest_csv(awaiting):
    """retest.csv: the same list as retest.json, one row per sample, for Excel."""
    rows = [[x["code"], x["name"], x["supplier"], x["date"], ", ".join(TEST_NAMES[k] for k in x["retest"]),
             blank(x["moisture"]), blank(x["vm_ar"]), blank(x["ash_ar"]), blank(x["total_moisture"]),
             "; ".join(x["flags"]), x["status"], "yes" if x["approval_outdated"] else ""]
            for x in awaiting]
    write_csv("retest.csv", ["Code", "Sample name", "Supplier", "Date", "Retest", "Moisture %", "VM (ar) %", "Ash (ar) %",
                             "Total moisture %", "Reason", "Status", "Approval outdated"], rows)


def when(iso):
    """2026-10-19T10:49:09+05:30 -> 19-Oct-2026 10:49 (the time as the phone saw it)."""
    try:
        return datetime.fromisoformat(iso).strftime("%d-%b-%Y %H:%M")
    except (TypeError, ValueError):
        return iso or ""


def write_corrections_csv(entries, names):
    """corrections.csv: corrections.json for Excel. A reset becomes one row per weight it cleared."""
    rows = []
    for e in reversed(entries):
        code = e.get("code", "")
        n = names.get(norm_code(code)) or {}
        if e.get("step"):
            cells = [(e["step"], e.get("old"), e.get("new"), e.get("saved") or {})]
        else:
            old, new, saved = e.get("old") or [None] * 3, e.get("new") or [None] * 3, e.get("saved") or {}
            cells = [(i + 1, old[i], new[i], saved.get(f"W{i + 1}") or {}) for i in range(3) if old[i] is not None]
            if not cells:  # only an imported result was cleared
                cells = [("", None, None, {})]
        for step, old, new, saved in cells:
            rows.append([when(e.get("at")), e.get("by", ""), e.get("action", ""), code, n.get("name", ""), e.get("test", ""),
                         f"W{step}" if step else "", blank(old), blank(new),
                         blank(e.get("oldResult")) if not e.get("step") else "",
                         saved.get("by", ""), when(saved.get("at")) if saved.get("at") else "", e.get("restoreOf", ""), e.get("id", "")])
    write_csv("corrections.csv", ["Changed at", "Changed by", "Action", "Code", "Sample name", "Test", "Weight",
                                  "Old value (g)", "New value (g)", "Old result %", "Old value saved by", "Old value saved at",
                                  "Restore of entry", "Entry id"], rows)


def write_csv(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    path = ROOT / name
    text = buf.getvalue()
    if path.exists() and path.read_text(encoding="utf-8-sig") == text:
        print(f"{name} unchanged")
        return
    path.write_text(text, encoding="utf-8-sig")  # BOM so Excel reads names correctly
    print(f"{name} written: {len(rows)} rows")


def write_if_changed(name, out, summary):

    path = ROOT / name
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        unchanged = {k: v for k, v in old.items() if k != "generated"} == {k: v for k, v in out.items() if k != "generated"}
    except (FileNotFoundError, json.JSONDecodeError):
        unchanged = False
    if unchanged:
        print(f"{name} unchanged")
        return
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{name} written: {summary}")


if __name__ == "__main__":
    main()
