"""Checks the hard suite's checkers before IO is graded by them: for every task, after its setup, (1) doing nothing must
FAIL its checks, and (2) a reference solution written by hand must PASS them. A checker that passes on nothing, or fails
on a right answer, would make the benchmark lie.

    .venv\\Scripts\\python.exe bench\\validate_hard.py [H01,H02,...]
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
SANDBOX = Path(tempfile.gettempdir()) / "io-bench-validate"

# reference solutions: Python run in the task folder (B), and the answer IO would give
REF = {
    "H01": ("open('fizzbuzz.py','w').write('import sys\\nfor i in range(1,int(sys.argv[1])+1):\\n    print(\"FizzBuzz\" if i%15==0 else \"Fizz\" if i%3==0 else \"Buzz\" if i%5==0 else i)\\n')", "The last line is FizzBuzz."),
    "H02": ("s=open('calc.py').read().replace(' + 1',''); open('calc.py','w').write(s)", "Fixed average."),
    "H03": ("open('test_slugify.py','w').write('import unittest\\nfrom slugify import slugify\\nclass T(unittest.TestCase):\\n'"
            "+'    def test_spaces(self): self.assertEqual(slugify(\"a b\"), \"a-b\")\\n'"
            "+'    def test_punct(self): self.assertEqual(slugify(\"a,b!\"), \"a-b\")\\n'"
            "+'    def test_case(self): self.assertEqual(slugify(\"AB\"), \"ab\")\\n'"
            "+'    def test_dashes(self): self.assertEqual(slugify(\"a  --  b\"), \"a-b\")\\n'"
            "+'    def test_trim(self): self.assertEqual(slugify(\"  !a! \"), \"a\")\\n')", "5 tests pass."),
    "H04": ("import os\nfor d,_,fs in os.walk('.'):\n  for f in fs:\n    if f.endswith('.py'):\n      p=os.path.join(d,f); s=open(p).read().replace('get_usr','get_user'); open(p,'w').write(s)", "Renamed."),
    "H05": ("open('wc.py','w').write('import sys\\ns=open(sys.argv[1]).read()\\nprint(s.count(\"\\\\n\"), len(s.split()), len(s))\\n')\ns=open('sample.txt').read()\nopen('_ans','w').write(f'{s.count(chr(10))} lines, {len(s.split())} words, {len(s)} characters')", "@_ans"),
    "H06": ("import subprocess,sys,time\nopen('srv.py','w').write('import json,time\\nfrom http.server import *\\nclass H(BaseHTTPRequestHandler):\\n def do_GET(s):\\n  b=json.dumps({\"status\":\"ok\"} if s.path==\"/health\" else {\"time\":time.strftime(\"%H:%M:%S\")}).encode()\\n  s.send_response(200); s.end_headers(); s.wfile.write(b)\\nHTTPServer((\"127.0.0.1\",8151),H).serve_forever()\\n')\n"
            "subprocess.Popen([sys.executable,'srv.py'], creationflags=0x08000000); time.sleep(1.5)", "Running on 8151."),
    "H07": ("import json\njson.dump([{'text':'buy milk','done':True},{'text':'call mom','done':False}],open('todo.json','w'))\n"
            "open('todo.py','w').write('import json\\nfor i,t in enumerate(json.load(open(\"todo.json\")),1): print(i,t[\"text\"],t[\"done\"])\\n')", "1 buy milk (done), 2 call mom"),
    "H08": ("s=open('report.py').read().replace('row[\"Region\"]','row[\"region\"]').replace('float(row[\"amount\"])','float(row[\"amount\"] or 0)'); open('report.py','w').write(s)", "Fixed."),
    "H09": ("open('slow.py','w').write('n=60000\\ns=bytearray([1])*n\\ns[0]=s[1]=0\\nfor i in range(2,int(n**.5)+1):\\n    if s[i]: s[i*i::i]=bytearray(len(s[i*i::i]))\\np=[i for i in range(n) if s[i]]\\nprint(len(p), sum(p), p[-1])\\n')", "Fast now."),
    "H10": ("import re\ne=sorted({m.lower() for m in re.findall(r'[\\w.+-]+@[\\w-]+(?:\\.[\\w-]+)*\\.[a-z]{2,}', open('emails.txt').read(), re.I)})\nopen('emails_out.txt','w').write('\\n'.join(e)+'\\n')", "Done."),
    "H11": ("import subprocess as sp\nfor c in (['git','init','-q'],['git','add','.'],['git','-c','user.name=b','-c','user.email=b@b','commit','-qm','initial'],['git','checkout','-qb','feature']): sp.run(c,check=True)\n"
            "open('NOTES.md','w').write('hello\\n')\nfor c in (['git','add','NOTES.md'],['git','-c','user.name=b','-c','user.email=b@b','commit','-qm','notes']): sp.run(c,check=True)", "feature has 2 commits"),
    "H12": ("import json\nd=json.load(open('data.json'))\nrows=''.join(f'<tr><td>{x[\"name\"]}</td><td>{x[\"qty\"]}</td><td>{x[\"price\"]}</td><td>{x[\"qty\"]*x[\"price\"]:.2f}</td></tr>' for x in d)\n"
            "open('report.html','w').write(f'<table><tr><th>n</th></tr>{rows}<tr><td>Total</td><td></td><td></td><td>{sum(x[\"qty\"]*x[\"price\"] for x in d):.2f}</td></tr></table>')", "Done."),
    "H13": ("open('convert.py','w').write('def c_to_f(c): return c*9/5+32\\ndef f_to_c(f): return (f-32)*5/9\\n')\nopen('test_convert.py','w').write('import unittest\\nfrom convert import *\\nclass T(unittest.TestCase):\\n    def test_a(self): self.assertEqual(c_to_f(100),212)\\n')", "Tests pass."),
    "H14": ("import urllib.request,re\nopen('fetch_title.py','w').write('import urllib')\nh=urllib.request.urlopen('https://example.com',timeout=30).read().decode()\nopen('title.txt','w').write(re.search('<title>(.*?)</title>',h).group(1))", "Saved."),
    "H15": ("import csv\nrev={}\nfor r in csv.DictReader(open('sales.csv')): rev[r['category']]=rev.get(r['category'],0)+int(r['units'])*float(r['unit_price'])\n"
            "w=csv.writer(open('summary.csv','w',newline='')); w.writerow(['category','revenue'])\nfor c in sorted(rev,key=rev.get,reverse=True): w.writerow([c,f'{rev[c]:.2f}'])\n"
            "open('_ans','w').write(max(rev,key=rev.get))", "@_ans"),
    "H16": ("import json\nu=json.load(open('users.json'))['users']\nopen('active_emails.txt','w').write('\\n'.join(sorted(x['email'] for x in u if x['active'] and x['age']>30))+'\\n')", "Done."),
    "H17": ("import re\nc={};n=0\nfor l in open('server.log'):\n  m=re.search(r'\"GET (\\S+) [^\"]*\" (\\d+)',l)\n  if m and m.group(2)=='500': n+=1; c[m.group(1)]=c.get(m.group(1),0)+1\n"
            "open('_ans','w').write(f'{n} requests returned 500; the most were on {max(c,key=c.get)}')", "@_ans"),
    "H18": ("import csv\nc={r['customer_id']:r for r in csv.DictReader(open('customers.csv'))}\nw=csv.writer(open('merged.csv','w',newline='')); w.writerow(['order_id','customer_id','name','city','total'])\n"
            "for o in csv.DictReader(open('orders.csv')): w.writerow([o['order_id'],o['customer_id'],c[o['customer_id']]['name'],c[o['customer_id']]['city'],o['total']])", "Done."),
    "H19": ("import csv\nseen=set();out=[]\nrows=list(csv.reader(open('contacts.csv')))\nfor r in rows[1:]:\n  if r[1].lower() not in seen: seen.add(r[1].lower()); out.append(r)\n"
            "csv.writer(open('contacts_clean.csv','w',newline='')).writerows([rows[0]]+out)", "Done."),
    "H20": ("import statistics as s\nx=[float(l) for l in open('numbers.txt').read().split()]\nopen('_ans','w').write(f'mean {s.mean(x):.2f}, median {s.median(x):.2f}, sd {s.pstdev(x):.2f}')", "@_ans"),
    "H21": ("import datetime as d\nn=sum(1 for i in range(90) if (d.date(2026,1,1)+d.timedelta(i)).weekday()<5)\nopen('_ans','w').write(f'There are {n} weekdays.')", "@_ans"),
    "H22": ("open('scores.md','w').write('| name | score |\\n|---|---|\\n| Amy | 93 |\\n| Cy | 93 |\\n| Bo | 88 |\\n| Zed | 71 |\\n| Di | 59 |\\n')", "Done."),
    "H23": ("import os,shutil\nfor f in os.listdir('.'):\n  if os.path.isfile(f): e=f.rsplit('.',1)[1]; os.makedirs(e,exist_ok=True); shutil.move(f,os.path.join(e,f))", "Done."),
    "H24": ("import os\nfor i in range(1,11): os.rename(f'IMG_{i:03d}.jpg',f'vacation_{i:02d}.jpg')", "Done."),
    "H25": ("import os\nfor f in ('beta.txt','zeta.txt','epsilon.txt'): os.remove(f)", "Deleted beta.txt, zeta.txt, epsilon.txt"),
    "H26": ("import zipfile,os\nz=zipfile.ZipFile('project.zip','w')\nfor d,_,fs in os.walk('project'):\n  for f in fs:\n    if not f.endswith('.log'): z.write(os.path.join(d,f))\nz.close()", "Done."),
    "H27": ("import zipfile\nzipfile.ZipFile('archive.zip').extractall('.')", "105 lines in total"),
    "H28": ("pass", "re_payment.txt, q3.md and old_thread.txt"),
    "H29": ("pass", "huge.log 879 KB, photo.raw 635 KB, data.bin 410 KB"),
    "H30": ("import shutil,os\nos.makedirs('backup')\nfor f in ('new1.txt','new2.txt'): shutil.copy2(os.path.join('src',f),'backup')", "Done."),
    "H31": ("import urllib.request,json,re\nrel=json.load(urllib.request.urlopen('https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false',timeout=30))\n"
            "v=max(tuple(map(int,m.group(1).split('.'))) for r in rel if (m:=re.fullmatch(r'Python (3\\.\\d+\\.\\d+)',r['name'])))\nopen('_ans','w').write('Python '+'.'.join(map(str,v)))", "@_ans"),
    "H32": ("open('example.txt','w').write('This domain is for use in illustrative examples in documents.')", "Saved."),
    "H33": ("pass", "The default branch is main; last pushed 2026-10-04."),
    "H34": ("pass", "RFC 9110"),
    "H35": ("pass", "1990, on Space Shuttle Discovery"),
    "H36": ("open('codes.txt','w').write('Swiss franc: CHF\\nSouth African rand: ZAR\\nIndian rupee: INR\\n')", "Saved."),
    "H37": ("f=[0,1]\nwhile len(f)<20: f.append(f[-1]+f[-2])\nopen('fib.txt','w').write(','.join(map(str,f)))", "Done."),
    "H38": ("import subprocess,sys,time\nopen('index.html','w').write('<a href=\"about.html\">About</a>')\nopen('about.html','w').write('<a href=\"index.html\">Home</a>')\n"
            "subprocess.Popen([sys.executable,'-m','http.server','8152','--bind','127.0.0.1'],creationflags=0x08000000); time.sleep(1.5)", "Serving on 8152."),
    "H39": ("import csv\nr=list(csv.DictReader(open('monthly.csv')))\nbars=''.join(f'<rect x=\"{i*30}\" y=\"0\" width=\"20\" height=\"{int(x[\"sales\"])//10}\"/><text x=\"{i*30}\" y=\"100\">{x[\"month\"]}</text>' for i,x in enumerate(r))\n"
            "open('chart.svg','w').write(f'<svg xmlns=\"http://www.w3.org/2000/svg\">{bars}</svg>')", "Done."),
    "H40": ("import csv\nrows=list(csv.DictReader(open('sales.csv')))\nrev={};bym={}\nfor r in rows:\n  v=int(r['units'])*float(r['unit_price']); rev[r['category']]=rev.get(r['category'],0)+v; bym[r['date'][5:7]]=bym.get(r['date'][5:7],0)+v\n"
            "names=['January','February','March','April','May','June','July','August','September','October','November','December']\n"
            "md=f'Total revenue: {sum(rev.values()):.2f}\\n\\nBest month: {names[int(max(bym,key=bym.get))-1]}\\n\\n| category | revenue |\\n|---|---|\\n'+''.join(f'| {c} | {v:.2f} |\\n' for c,v in rev.items())\nopen('report.md','w').write(md)", "Done."),
}


def run_py(code: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=180,
                          encoding="utf-8", errors="replace")


def checks_pass(t: dict, answer: str, env: dict) -> tuple[bool, list[str]]:
    sys.path.insert(0, str(HERE))
    import run_bench
    run_bench.SANDBOX = SANDBOX
    notes, ok = [], True
    for c in t["checks"]:
        passed, seen = run_bench.check(c, {"answer": answer, "events_full": [], "status": "done", "secs": 1}, {"mode": "current"})
        ok = ok and passed
        notes.append(f"{c['type']}: {'pass' if passed else 'FAIL'} ({seen[:120]})")
    return ok, notes


def kill_port(port: int) -> None:
    import psutil
    for conn in psutil.net_connections("tcp"):
        if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port and conn.pid:
            try:
                psutil.Process(conn.pid).kill()
            except psutil.Error:
                pass


def main() -> int:
    tasks = json.loads((HERE / "tasks_hard.json").read_text(encoding="utf-8"))["tasks"]
    only = set(sys.argv[1].split(",")) if len(sys.argv) > 1 else None
    env = {**os.environ, "BENCH": str(SANDBOX)}
    bad = 0
    for t in tasks:
        if only and t["id"] not in only:
            continue
        SANDBOX.mkdir(exist_ok=True)
        for s in t["setup"]:
            if s["op"] == "python":
                r = run_py(s["code"], SANDBOX, env)
                if r.returncode:
                    print(f"{t['id']}: SETUP BROKEN {r.stderr[-300:]}"); bad += 1; break
        else:
            empty_ok, empty_notes = checks_pass(t, "", env)
            code, answer = REF[t["id"]]
            folder = SANDBOX / t["id"]
            r = run_py(code, folder, env)
            if answer.startswith("@"):
                ans_file = folder / answer[1:]
                answer = ans_file.read_text(encoding="utf-8") if ans_file.exists() else ""
                (folder / "_ans").unlink(missing_ok=True)
            ref_ok, ref_notes = checks_pass(t, answer, env)
            for td in t.get("teardown", []):
                if "port" in td:
                    kill_port(td["port"])
            verdict = "ok" if (not empty_ok and ref_ok and not r.returncode) else "BAD"
            bad += verdict == "BAD"
            print(f"{t['id']} {verdict}: nothing done -> {'passes (checker too weak!)' if empty_ok else 'fails'}; "
                  f"reference -> {'passes' if ref_ok else 'FAILS'}" + (f" (reference crashed: {r.stderr[-200:]})" if r.returncode else ""))
            if verdict == "BAD":
                print("   empty:", empty_notes, "\n   ref:", ref_notes)
    print(f"{bad} bad")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
