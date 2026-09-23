"""Parse statement files and print the reconciliation summary. READ-ONLY, PROPOSE ONLY.
Usage: python3 run_statements.py <carrier>=<path> [...]    e.g.  rps=/downloads/rps.pdf xsb=/downloads/xsb.pdf
       python3 run_statements.py --gmail search.json          (gmail search JSON; attachments already downloaded
                                                               to /downloads/<filename>; sender picks the parser)"""
import sys, json, os
from registry import parse_file, carrier_for
from report import summarize

def jobs(argv):
    if argv[:1] == ["--gmail"]:
        for e in json.load(open(argv[1]))["emails"]:
            c = carrier_for(e["from"])
            for a in e.get("attachments", []):
                if not a["filename"].lower().endswith((".pdf", ".xlsx")) or "image" in a["filename"].lower(): continue
                if c is None: print(f"- UNKNOWN SENDER {e['from']}: {a['filename']} (no parser; not guessed)"); continue
                if c == "one80" and a["filename"].lower().endswith(".pdf"): continue   # xlsx is the source; pdf is a copy
                yield c, os.path.join("/downloads", a["filename"])
    else:
        for x in argv: c, p = x.split("=", 1); yield c, p

if __name__ == "__main__":
    for c, p in jobs(sys.argv[1:]):
        try: print(summarize(parse_file(c, p)))
        except Exception as ex: print(f"## {c} {p}\n- PARSE FAILED (stopped, not guessed): {ex}")
        print()
