"""The hard suite: work a person would hand a capable assistant (code, debugging, data, files, web, multi-step builds),
each checked by a script against the result, never by trusting the answer. Writes bench/tasks_hard.json:

    .venv\\Scripts\\python.exe bench\\hard_tasks.py
    .venv\\Scripts\\python.exe bench\\run_bench.py --suite hard --director on

Every task works in its own folder %TEMP%\\io-bench\\<id> (made fresh by its setup). In checker code: B is that folder,
A is IO's answer, run(...) runs a command there, fail(msg) fails the check with msg as what was observed."""
import json
from pathlib import Path

HERE = Path(__file__).parent


def prelude(tid: str) -> str:
    return (f"import os, sys, json, subprocess, re, csv, hashlib, statistics, zipfile, time\n"
            f"B = os.path.join(os.environ['BENCH'], '{tid}'); A = os.environ['ANSWER']\n"
            "def run(*a, timeout=60):\n"
            "    return subprocess.run(list(a), cwd=B, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace')\n"
            "def fail(m):\n    print(m); sys.exit(1)\n"
            "def read(name):\n    p = os.path.join(B, name)\n    return open(p, encoding='utf-8-sig').read() if os.path.exists(p) else fail(f'{name} is missing')\n")


def fresh(tid: str, body: str = "") -> list:
    """Setup: the task's folder made empty, then body (Python, with B set) fills it."""
    code = (f"import os, shutil, json, random, csv, zipfile, time\nB = os.path.join(os.environ['BENCH'], '{tid}')\n"
            "def _rm(f, p, e):\n    os.chmod(p, 0o700); f(p)\n"            "shutil.rmtree(B, onexc=_rm) if os.path.exists(B) else None; os.makedirs(B)\n"
            "def put(name, text, ago_days=0):\n"
            "    p = os.path.join(B, name); os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "    open(p, 'w', encoding='utf-8', newline='\\n').write(text)\n"
            "    if ago_days: t = time.time() - ago_days * 86400; os.utime(p, (t, t))\n" + body)
    return [{"op": "sandbox"}, {"op": "python", "code": code}]


def task(tid, cat, text, setup_body="", checks=(), timeout=900, teardown=None, max_steps=60):
    t = {"id": tid, "cat": cat, "suites": ["hard"], "text": text.replace("{dir}", f"%TEMP%\\io-bench\\{tid}"),
         "timeout": timeout, "max_steps": max_steps, "setup": fresh(tid, setup_body), "checks": list(checks)}
    if teardown:
        t["teardown"] = teardown
    return t


def verify(tid: str, code: str) -> dict:
    return {"type": "verify", "code": prelude(tid) + code}


SALES = """
random.seed(7)
cats = ['Hardware', 'Software', 'Services', 'Training']
rows = [['date', 'product', 'category', 'units', 'unit_price']]
for i in range(240):
    m = 1 + i % 12
    rows.append([f'2026-{m:02d}-{1 + (i * 7) % 28:02d}', f'P{i % 17}', cats[(i * 5 + m) % 4], random.randint(1, 20), round(random.uniform(5, 300), 2)])
with open(os.path.join(B, 'sales.csv'), 'w', newline='') as f:
    csv.writer(f).writerows(rows)
"""
SALES_TRUTH = """
rows = list(csv.DictReader(open(os.path.join(B, 'sales.csv'))))
rev = {}
for r in rows:
    rev[r['category']] = rev.get(r['category'], 0) + int(r['units']) * float(r['unit_price'])
"""

TASKS = [
    # ---------- code ----------
    task("H01", "code", "In {dir}, write fizzbuzz.py that prints FizzBuzz from 1 to N, where N is its first command-line argument "
         "(Fizz for multiples of 3, Buzz for 5, FizzBuzz for both). Run it with 15 and tell me the last line it prints.",
         checks=[verify("H01", "r = run(sys.executable, 'fizzbuzz.py', '30')\n"
                        "want = ['FizzBuzz' if i % 15 == 0 else 'Fizz' if i % 3 == 0 else 'Buzz' if i % 5 == 0 else str(i) for i in range(1, 31)]\n"
                        "got = [l.strip() for l in r.stdout.splitlines() if l.strip()]\n"
                        "if got != want: fail(f'output for 30: {got[:8]}...')\n"
                        "if 'fizzbuzz' not in A.lower(): fail('answer lacks the last line FizzBuzz')\nprint('ok')")]),
    task("H02", "code", "The tests in {dir} are failing. Find the bug in calc.py and fix it so all the tests pass. Don't change test_calc.py.",
         setup_body="put('calc.py', 'def add(a, b):\\n    return a + b\\n\\ndef average(xs):\\n    return sum(xs) / len(xs) + 1\\n\\n"
                    "def clamp(x, lo, hi):\\n    return max(lo, min(x, hi))\\n')\n"
                    "put('test_calc.py', 'import unittest\\nfrom calc import add, average, clamp\\n\\nclass T(unittest.TestCase):\\n"
                    "    def test_add(self): self.assertEqual(add(2, 3), 5)\\n    def test_average(self): self.assertEqual(average([2, 4, 6]), 4)\\n"
                    "    def test_clamp(self): self.assertEqual(clamp(15, 0, 10), 10)\\n\\nif __name__ == \"__main__\": unittest.main()\\n')\n",
         checks=[verify("H02", "t = read('test_calc.py')\nif 'average([2, 4, 6]), 4' not in t: fail('test_calc.py was changed')\n"
                        "r = run(sys.executable, '-m', 'unittest', '-q', 'test_calc')\nif r.returncode: fail('tests fail: ' + r.stderr[-200:])\n"
                        "if '+ 1' in read('calc.py'): fail('the bug is still in calc.py')\nprint('ok')")]),
    task("H03", "code", "Write unittest tests for slugify.py in {dir} as test_slugify.py: at least 5 test methods covering its behaviour "
         "(spaces, punctuation, case, repeated dashes, leading/trailing junk). Run them and make sure they pass.",
         setup_body="put('slugify.py', 'import re\\n\\ndef slugify(text):\\n    s = re.sub(r\"[^a-z0-9]+\", \"-\", text.lower())\\n    return s.strip(\"-\")\\n')\n",
         checks=[verify("H03", "t = read('test_slugify.py')\nn = len(re.findall(r'def test_', t))\nif n < 5: fail(f'only {n} test methods')\n"
                        "r = run(sys.executable, '-m', 'unittest', '-q', 'test_slugify')\nif r.returncode: fail('tests fail: ' + r.stderr[-200:])\n"
                        "orig = read('slugify.py')\nopen(os.path.join(B, 'slugify.py'), 'w').write('def slugify(text):\\n    return text.lower().replace(\" \", \"-\")\\n')\n"
                        "r2 = run(sys.executable, '-m', 'unittest', '-q', 'test_slugify')\nopen(os.path.join(B, 'slugify.py'), 'w').write(orig)\n"
                        "if r2.returncode == 0: fail('the tests also pass on a broken slugify: they check too little')\nprint('ok')")]),
    task("H04", "code", "In the project in {dir}, rename the function get_usr to get_user everywhere (definition and every use), "
         "and keep the tests passing.",
         setup_body="put('users.py', 'def get_usr(uid):\\n    return {\"id\": uid, \"name\": f\"user{uid}\"}\\n')\n"
                    "put('service.py', 'from users import get_usr\\n\\ndef display(uid):\\n    return get_usr(uid)[\"name\"].title()\\n')\n"
                    "put('api/handlers.py', 'import sys, os\\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\\nfrom users import get_usr\\n\\n"
                    "def handle(uid):\\n    u = get_usr(uid)\\n    return {\"ok\": True, \"user\": u}\\n')\n"
                    "put('test_all.py', 'import unittest\\nfrom service import display\\nfrom api.handlers import handle\\n\\nclass T(unittest.TestCase):\\n"
                    "    def test_display(self): self.assertEqual(display(3), \"User3\")\\n    def test_handle(self): self.assertTrue(handle(1)[\"ok\"])\\n')\n",
         checks=[verify("H04", "left = []\nfor d, _, fs in os.walk(B):\n    for f in fs:\n        if f.endswith('.py') and 'get_usr' in open(os.path.join(d, f), encoding='utf-8').read(): left.append(f)\n"
                        "if left: fail(f'get_usr still in {left}')\nif 'def get_user' not in read('users.py'): fail('no def get_user')\n"
                        "r = run(sys.executable, '-m', 'unittest', '-q', 'test_all')\nif r.returncode: fail('tests fail: ' + r.stderr[-200:])\nprint('ok')")]),
    task("H05", "code", "Write wc.py in {dir}: it prints the number of lines, words and characters of the file named by its first argument. "
         "Run it on sample.txt and tell me the three numbers.",
         setup_body="put('sample.txt', 'The quick brown fox\\njumps over\\nthe lazy dog.\\n\\nEnd of sample text here\\n')\n",
         checks=[verify("H05", "s = read('sample.txt')\nlines, words, chars = s.count('\\n'), len(s.split()), len(s)\n"
                        "r = run(sys.executable, 'wc.py', 'sample.txt')\nnums = [int(x) for x in re.findall(r'\\d+', r.stdout)]\n"
                        "if not {lines, words} <= set(nums): fail(f'wc.py printed {r.stdout.strip()!r}; want {lines} lines, {words} words')\n"
                        "if not all(re.search(rf'\\b{n}\\b', A) for n in (lines, words)): fail('answer lacks the counts')\nprint('ok')")]),
    task("H06", "code", "In {dir}, write a tiny web server with Python's standard library that answers GET /health with the JSON "
         "{\"status\": \"ok\"} and GET /time with the current time as JSON. Start it on port 8151 so it keeps running after you finish.",
         checks=[{"type": "http", "url": "http://127.0.0.1:8151/health", "contains": "\"status\"\\s*:\\s*\"ok\""},
                 {"type": "http", "url": "http://127.0.0.1:8151/time", "contains": "\\d{1,2}:\\d{2}"}],
         teardown=[{"port": 8151}]),
    task("H07", "code", "In {dir}, make todo.py, a command-line to-do list that stores its items in todo.json, with the commands "
         "add <text>, list and done <number>. Use it to add 'buy milk' and 'call mom', mark the first one done, and show me the list.",
         checks=[verify("H07", "d = json.loads(read('todo.json'))\nitems = d if isinstance(d, list) else d.get('items') or d.get('todos') or list(d.values())[0]\n"
                        "txt = json.dumps(items).lower()\nif 'buy milk' not in txt or 'call mom' not in txt: fail(f'todo.json: {txt[:150]}')\n"
                        "milk = next(i for i in items if 'buy milk' in json.dumps(i).lower())\n"
                        "if not re.search(r'(done|completed|finished)\"?\\s*:\\s*true', json.dumps(milk).lower()): fail(f'buy milk not done: {milk}')\n"
                        "r = run(sys.executable, 'todo.py', 'list')\nif 'call mom' not in r.stdout.lower(): fail('list shows ' + r.stdout[:120])\nprint('ok')")]),
    task("H08", "code", "report.py in {dir} crashes when I run it. Fix it so it prints the total sales per region from data.csv.",
         setup_body="put('data.csv', 'region,amount\\nNorth,120.5\\nSouth,80\\nNorth,30\\nEast,55.25\\nSouth,20\\nWest,\\nEast,10\\n')\n"
                    "put('report.py', 'import csv\\n\\ntotals = {}\\nfor row in csv.DictReader(open(\"data.csv\")):\\n"
                    "    totals[row[\"Region\"]] = totals.get(row[\"Region\"], 0) + float(row[\"amount\"])\\n"
                    "for region, total in sorted(totals.items()):\\n    print(f\"{region}: {total:.2f}\")\\n')\n",
         checks=[verify("H08", "r = run(sys.executable, 'report.py')\nif r.returncode: fail('still crashes: ' + r.stderr[-200:])\n"
                        "want = {'North': 150.5, 'South': 100.0, 'East': 65.25}\n"
                        "for k, v in want.items():\n    if not re.search(rf'{k}\\D*{v:.2f}', r.stdout) and not re.search(rf'{k}\\D*{v:g}\\b', r.stdout): fail(f'{k} wrong in {r.stdout!r}')\nprint('ok')")]),
    task("H09", "code", "slow.py in {dir} takes far too long. Make it finish in under 2 seconds while printing exactly the same output.",
         setup_body="put('slow.py', 'def is_prime(n):\\n    if n < 2:\\n        return False\\n    for d in range(2, n):\\n"
                    "        if n % d == 0:\\n            return False\\n    return True\\n\\nprimes = [n for n in range(2, 60000) if is_prime(n)]\\n"
                    "print(len(primes), sum(primes), primes[-1])\\n')\n",
         checks=[verify("H09", "t = time.time(); r = run(sys.executable, 'slow.py', timeout=30); secs = time.time() - t\n"
                        "if r.stdout.strip() != '6057 171848738 59999': fail(f'output {r.stdout.strip()!r}')\n"
                        "if secs > 2: fail(f'took {secs:.1f}s')\nprint(f'ok in {secs:.2f}s')")]),
    task("H10", "code", "Extract every email address from emails.txt in {dir} into emails_out.txt: unique, lowercase, sorted, one per line.",
         setup_body="put('emails.txt', 'Contact Ana <ana@example.com> or BOB@Example.org.\\nOld: ana@example.com, x@y\\n"
                    "Support: help-desk@corp.co.uk; sales+eu@corp.co.uk\\nNot emails: @nope, user@, a@b\\nRepeat: Bob@example.org\\n')\n",
         checks=[verify("H10", "got = [l.strip() for l in read('emails_out.txt').splitlines() if l.strip()]\n"
                        "want = sorted({'ana@example.com', 'bob@example.org', 'help-desk@corp.co.uk', 'sales+eu@corp.co.uk'})\n"
                        "if got != want: fail(f'got {got}')\nprint('ok')")]),
    task("H11", "code", "In {dir}, make a git repository, commit the files that are there with the message 'initial', then create a "
         "branch called feature and on it commit a new file NOTES.md containing hello. Tell me how many commits feature has.",
         setup_body="put('main.py', 'print(\"hi\")\\n'); put('README.md', '# demo\\n')\n",
         checks=[verify("H11", "r = run('git', 'log', 'feature', '--format=%s')\nif r.returncode: fail('no feature branch: ' + r.stderr[-150:])\n"
                        "subs = r.stdout.split('\\n')\nif 'initial' not in subs or len([s for s in subs if s]) != 2: fail(f'feature log: {subs}')\n"
                        "f = run('git', 'show', 'feature:NOTES.md')\nif 'hello' not in f.stdout.lower(): fail('NOTES.md not committed on feature')\n"
                        "if not re.search(r'\\b2\\b|\\btwo\\b', A.lower()): fail('answer lacks 2')\nprint('ok')")]),
    task("H12", "code", "Turn data.json in {dir} into report.html: a table with one row per item (name, quantity, price, line total) "
         "and a final row with the grand total.",
         setup_body="put('data.json', json.dumps([{'name': 'Widget', 'qty': 3, 'price': 4.5}, {'name': 'Gadget', 'qty': 1, 'price': 19.99}, "
                    "{'name': 'Doohickey', 'qty': 10, 'price': 0.75}]))\n",
         checks=[verify("H12", "h = read('report.html').replace(',', '')\n"
                        "for s in ('Widget', 'Gadget', 'Doohickey', '13.5', '19.99', '7.5', '40.99'):\n    if s not in h: fail(f'{s} missing from report.html')\n"
                        "if h.lower().count('<tr') < 5: fail('fewer than 5 table rows')\nprint('ok')")]),
    task("H13", "code", "Write convert.py in {dir} with functions c_to_f and f_to_c, plus unittest tests in test_convert.py. Run the tests.",
         checks=[verify("H13", "sys.path.insert(0, B)\nimport convert\n"
                        "for c, f in ((0, 32), (100, 212), (-40, -40), (37, 98.6)):\n"
                        "    if abs(convert.c_to_f(c) - f) > 0.01 or abs(convert.f_to_c(f) - c) > 0.01: fail(f'wrong at {c}C/{f}F')\n"
                        "r = run(sys.executable, '-m', 'unittest', '-q', 'test_convert')\nif r.returncode or 'Ran 0' in r.stderr: fail('tests: ' + r.stderr[-150:])\nprint('ok')")]),
    task("H14", "code", "Write fetch_title.py in {dir} that downloads https://example.com and saves the page's <title> to title.txt. Run it.",
         checks=[verify("H14", "t = read('title.txt')\nif 'example domain' not in t.lower(): fail(f'title.txt: {t[:80]!r}')\n"
                        "if 'urllib' not in read('fetch_title.py') and 'requests' not in read('fetch_title.py'): fail('the script does not download')\nprint('ok')")]),
    # ---------- data ----------
    task("H15", "data", "Using sales.csv in {dir} (revenue = units x unit_price), write summary.csv with the total revenue per category, "
         "sorted from highest to lowest, and tell me the top category.", setup_body=SALES,
         checks=[verify("H15", SALES_TRUTH + "got = list(csv.reader(open(os.path.join(B, 'summary.csv'))))\nbody = [r for r in got if r and r[0] in rev]\n"
                        "order = sorted(rev, key=rev.get, reverse=True)\nif [r[0] for r in body] != order: fail(f'order {[r[0] for r in body]} want {order}')\n"
                        "for r in body:\n    if abs(float(r[1].replace(',', '')) - rev[r[0]]) > 0.05: fail(f'{r} want {rev[r[0]]:.2f}')\n"
                        "if order[0].lower() not in A.lower(): fail('answer lacks the top category')\nprint('ok')")]),
    task("H16", "data", "From users.json in {dir}, write active_emails.txt with the emails of active users older than 30, one per line, "
         "in alphabetical order.",
         setup_body="random.seed(3)\nus = [{'name': f'u{i}', 'email': f'user{i:02d}@mail.test', 'age': random.randint(18, 60), 'active': random.random() > 0.4} for i in range(40)]\n"
                    "put('users.json', json.dumps({'users': us}, indent=1))\n",
         checks=[verify("H16", "us = json.loads(read('users.json'))['users']\nwant = sorted(u['email'] for u in us if u['active'] and u['age'] > 30)\n"
                        "got = [l.strip() for l in read('active_emails.txt').splitlines() if l.strip()]\nif got != want: fail(f'got {len(got)} lines, want {len(want)}: {got[:3]}')\nprint('ok')")]),
    task("H17", "data", "Look at server.log in {dir}: how many requests returned status 500, and which URL path had the most 500s? "
         "Answer with both.",
         setup_body="random.seed(11)\npaths = ['/api/login', '/api/orders', '/api/cart', '/home', '/api/search']\nlines = []\n"
                    "for i in range(800):\n    p = random.choice(paths); s = random.choice([200] * 12 + [404, 500, 500 if p == '/api/orders' else 200])\n"
                    "    lines.append(f'10.0.0.{i % 50} - - [04/Oct/2026:10:{i % 60:02d}:00] \"GET {p} HTTP/1.1\" {s} {random.randint(100, 9000)}')\n"
                    "put('server.log', '\\n'.join(lines) + '\\n')\n",
         checks=[verify("H17", "c = {}\nn = 0\nfor l in read('server.log').splitlines():\n    m = re.search(r'\"GET (\\S+) [^\"]*\" (\\d+)', l)\n"
                        "    if m and m.group(2) == '500': n += 1; c[m.group(1)] = c.get(m.group(1), 0) + 1\n"
                        "top = max(c, key=c.get)\nif not re.search(rf'\\b{n}\\b', A): fail(f'answer lacks {n}')\nif top not in A: fail(f'answer lacks {top}')\nprint('ok')")]),
    task("H18", "data", "Join customers.csv and orders.csv in {dir} on customer_id into merged.csv with the columns order_id, customer_id, "
         "name, city, total â€” one row per order.",
         setup_body="put('customers.csv', 'customer_id,name,city\\n1,Ana,Lisbon\\n2,Ben,Oslo\\n3,Cai,Lima\\n')\n"
                    "put('orders.csv', 'order_id,customer_id,total\\n101,2,19.5\\n102,1,7\\n103,2,42\\n104,3,3.25\\n')\n",
         checks=[verify("H18", "rows = list(csv.DictReader(open(os.path.join(B, 'merged.csv'), encoding='utf-8-sig')))\n"
                        "if len(rows) != 4: fail(f'{len(rows)} rows')\nby = {r['order_id']: r for r in rows}\n"
                        "if by['103']['name'] != 'Ben' or by['104']['city'] != 'Lima' or float(by['101']['total']) != 19.5: fail(f'rows: {rows}')\nprint('ok')")]),
    task("H19", "data", "contacts.csv in {dir} has duplicate people (same email, ignoring case). Write contacts_clean.csv keeping only the "
         "first row for each email, same columns and order.",
         setup_body="put('contacts.csv', 'name,email,phone\\nAna,ana@x.com,1\\nBen,ben@x.com,2\\nAna B,ANA@x.com,3\\nCai,cai@x.com,4\\nBen,Ben@X.com,5\\nDee,dee@x.com,6\\n')\n",
         checks=[verify("H19", "rows = list(csv.reader(open(os.path.join(B, 'contacts_clean.csv'), encoding='utf-8-sig')))\n"
                        "if rows[0] != ['name', 'email', 'phone']: fail(f'header {rows[0]}')\n"
                        "if [r[2] for r in rows[1:]] != ['1', '2', '4', '6']: fail(f'kept phones {[r[2] for r in rows[1:]]}')\nprint('ok')")]),
    task("H20", "data", "For the numbers in numbers.txt in {dir}, what are the mean, the median and the population standard deviation, "
         "each to 2 decimal places?",
         setup_body="random.seed(5)\nput('numbers.txt', '\\n'.join(str(random.randint(1, 500)) for _ in range(101)) + '\\n')\n",
         checks=[verify("H20", "xs = [float(l) for l in read('numbers.txt').split()]\n"
                        "for v in (statistics.mean(xs), statistics.median(xs), statistics.pstdev(xs)):\n"
                        "    if f'{v:.2f}' not in A.replace(',', ''): fail(f'answer lacks {v:.2f}')\nprint('ok')")]),
    task("H21", "data", "How many weekdays (Monday to Friday) are there from 2026-01-01 to 2026-03-31, counting both days? Work it out "
         "exactly (write code if you like) and tell me the number.",
         checks=[verify("H21", "import datetime\nd0 = datetime.date(2026, 1, 1)\nn = sum(1 for i in range(90) if (d0 + datetime.timedelta(i)).weekday() < 5)\n"
                        "if not re.search(rf'\\b{n}\\b', A): fail(f'answer lacks {n}')\nprint('ok', n)")]),
    task("H22", "data", "Make scores.md in {dir}: a Markdown table of scores.csv (name and score columns), highest score first.",
         setup_body="put('scores.csv', 'name,score\\nZed,71\\nAmy,93\\nBo,88\\nCy,93\\nDi,59\\n')\n",
         checks=[verify("H22", "md = read('scores.md')\nrows = [l for l in md.splitlines() if l.strip().startswith('|')]\n"
                        "if len(rows) < 7: fail(f'{len(rows)} table lines (want header, separator and 5 rows)')\n"
                        "names = [re.findall(r'\\|\\s*([A-Za-z]+)\\s*\\|', r)[0] for r in rows[2:7]]\n"
                        "if names[2:] != ['Bo', 'Zed', 'Di'] or set(names[:2]) != {'Amy', 'Cy'}: fail(f'order {names}')\nprint('ok')")]),
    # ---------- files ----------
    task("H23", "files", "Organize the files in {dir} into subfolders by file extension (txt files into a txt folder, csv into csv, and so on).",
         setup_body="for n in ['a.txt', 'b.txt', 'c.csv', 'd.png', 'e.csv', 'f.md', 'g.txt']: put(n, n)\n",
         checks=[verify("H23", "want = {'a.txt': 'txt', 'b.txt': 'txt', 'g.txt': 'txt', 'c.csv': 'csv', 'e.csv': 'csv', 'd.png': 'png', 'f.md': 'md'}\n"
                        "for f, sub in want.items():\n    if not os.path.isfile(os.path.join(B, sub, f)): fail(f'{f} not in {sub}/')\n"
                        "left = [f for f in os.listdir(B) if os.path.isfile(os.path.join(B, f))]\nif left: fail(f'still loose: {left}')\nprint('ok')")]),
    task("H24", "files", "Rename the photos in {dir} from IMG_001.jpg ... IMG_010.jpg to vacation_01.jpg ... vacation_10.jpg (same order).",
         setup_body="for i in range(1, 11): put(f'IMG_{i:03d}.jpg', f'photo {i}')\n",
         checks=[verify("H24", "for i in range(1, 11):\n    p = os.path.join(B, f'vacation_{i:02d}.jpg')\n"
                        "    if not os.path.isfile(p) or open(p).read() != f'photo {i}': fail(f'vacation_{i:02d}.jpg wrong or missing')\n"
                        "if any(f.startswith('IMG_') for f in os.listdir(B)): fail('IMG_ files left')\nprint('ok')")]),
    task("H25", "files", "Some files in {dir} are exact duplicates of each other (same content). Delete the extra copies, keeping the "
         "alphabetically first name of each set, and tell me which you deleted.",
         setup_body="put('alpha.txt', 'same A'); put('beta.txt', 'same A'); put('gamma.txt', 'unique'); put('delta.txt', 'same B');"
                    " put('epsilon.txt', 'same B'); put('zeta.txt', 'same A')\n",
         checks=[verify("H25", "have = sorted(os.listdir(B))\nif have != ['alpha.txt', 'delta.txt', 'gamma.txt']: fail(f'left: {have}')\nprint('ok')")]),
    task("H26", "files", "Zip the folder {dir}\\project into {dir}\\project.zip, leaving out the .log files.",
         setup_body="put('project/app.py', 'x=1'); put('project/lib/util.py', 'y=2'); put('project/debug.log', 'noise'); put('project/lib/old.log', 'noise');"
                    " put('project/README.md', 'r')\n",
         checks=[verify("H26", "names = [n.replace('\\\\', '/') for n in zipfile.ZipFile(os.path.join(B, 'project.zip')).namelist() if not n.endswith('/')]\n"
                        "if any(n.endswith('.log') for n in names): fail(f'logs inside: {names}')\n"
                        "if not all(any(n.endswith(x) for n in names) for x in ('app.py', 'lib/util.py', 'README.md')): fail(f'missing files: {names}')\nprint('ok')")]),
    task("H27", "files", "Extract archive.zip in {dir} and tell me how many lines all the .txt files inside have in total.",
         setup_body="z = zipfile.ZipFile(os.path.join(B, 'archive.zip'), 'w')\nfor i in range(1, 6): z.writestr(f'docs/part{i}.txt', '\\n'.join(f'line {j}' for j in range(i * 7)) + '\\n')\n"
                    "z.writestr('docs/skip.csv', 'a\\nb\\n'); z.close()\n",
         checks=[verify("H27", "n = sum(i * 7 for i in range(1, 6))\nif not re.search(rf'\\b{n}\\b', A): fail(f'answer lacks {n}')\n"
                        "if not any(f.endswith('.txt') for _, _, fs in os.walk(B) for f in fs): fail('nothing extracted')\nprint('ok')")]),
    task("H28", "files", "Which files under {dir} mention invoice 4471? Give me their names.",
         setup_body="random.seed(2)\nhits = ['mail/re_payment.txt', 'notes/q3.md', 'archive/2025/old_thread.txt']\n"
                    "for i in range(30): put(f'misc/file{i}.txt', f'invoice {4400 + i} paid\\n')\n"
                    "for h in hits: put(h, 'About Invoice 4471: overdue.\\n')\n",
         checks=[verify("H28", "for n in ('re_payment', 'q3', 'old_thread'):\n    if n not in A: fail(f'answer lacks {n}')\n"
                        "if re.search(r'file\\d+\\.txt', A): fail('answer lists files that do not mention it')\nprint('ok')")]),
    task("H29", "files", "What are the 3 largest files under {dir}, and how big is each?",
         setup_body="sizes = {'logs/huge.log': 900000, 'img/photo.raw': 650000, 'db/data.bin': 420000, 'db/index.bin': 90000, 'a.txt': 10}\n"
                    "for n, s in sizes.items():\n    p = os.path.join(B, n); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, 'wb').write(b'x' * s)\n",
         checks=[verify("H29", "for n in ('huge.log', 'photo.raw', 'data.bin'):\n    if n not in A: fail(f'answer lacks {n}')\n"
                        "if 'index.bin' in A: fail('index.bin is not in the top 3')\nprint('ok')")]),
    task("H30", "files", "Copy only the files in {dir}\\src that were changed in the last 7 days into {dir}\\backup.",
         setup_body="put('src/new1.txt', 'n1'); put('src/new2.txt', 'n2', 2); put('src/old1.txt', 'o1', 30); put('src/old2.txt', 'o2', 12)\n",
         checks=[verify("H30", "have = sorted(os.listdir(os.path.join(B, 'backup')))\nif have != ['new1.txt', 'new2.txt']: fail(f'backup has {have}')\n"
                        "if sorted(os.listdir(os.path.join(B, 'src'))) != ['new1.txt', 'new2.txt', 'old1.txt', 'old2.txt']: fail('src was changed')\nprint('ok')")]),
    # ---------- web ----------
    task("H31", "web", "What is the latest stable Python 3 release on python.org right now? Give me the exact version number.",
         checks=[verify("H31", "import urllib.request\nrel = json.load(urllib.request.urlopen('https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false', timeout=30))\n"
                        "vs = []\nfor r in rel:\n    m = re.fullmatch(r'Python (3\\.\\d+\\.\\d+)', r['name'])\n    if m: vs.append(tuple(int(x) for x in m.group(1).split('.')))\n"
                        "top = '.'.join(map(str, max(vs)))\nif top not in A: fail(f'answer lacks {top}')\nprint('ok', top)")]),
    task("H32", "web", "Read https://example.com and save the text of its main paragraph to {dir}\\example.txt.",
         checks=[verify("H32", "t = read('example.txt')\nif 'domain' not in t.lower() or 'example' not in t.lower(): fail(f'example.txt: {t[:100]!r}')\nprint('ok')")]),
    task("H33", "web", "What is the default branch of the GitHub repository chrissaba/IO, and when was it last pushed to (date)?",
         checks=[{"type": "answer_regex", "pattern": "\\bmain\\b"}, {"type": "answer_regex", "pattern": "2026"}]),
    task("H34", "web", "Which RFC number defines HTTP Semantics (the 2022 edition that obsoletes RFC 7231)?",
         checks=[{"type": "answer_regex", "pattern": "\\b9110\\b"}]),
    task("H35", "web", "On Wikipedia, in what year was the Hubble Space Telescope launched, and on which Space Shuttle?",
         checks=[{"type": "answer_regex", "pattern": "1990"}, {"type": "answer_regex", "pattern": "discovery"}]),
    task("H36", "web", "Find the ISO 4217 currency codes for the Swiss franc, the South African rand and the Indian rupee, and save them "
         "to {dir}\\codes.txt as lines like 'Swiss franc: CHF'.",
         checks=[verify("H36", "t = read('codes.txt').upper()\nfor c in ('CHF', 'ZAR', 'INR'):\n    if c not in t: fail(f'{c} missing: {t[:120]!r}')\nprint('ok')")]),
    # ---------- multi-step ----------
    task("H37", "multi", "In {dir}, write fib.py that writes the first 20 Fibonacci numbers (starting 0, 1) to fib.txt, comma separated, "
         "and run it.",
         checks=[verify("H37", "want = [0, 1]\nwhile len(want) < 20: want.append(want[-1] + want[-2])\n"
                        "got = [int(x) for x in re.findall(r'\\d+', read('fib.txt'))]\nif got != want: fail(f'fib.txt {got[:6]}...')\nprint('ok')")]),
    task("H38", "multi", "Make a two-page static site in {dir}: index.html and about.html, each linking to the other. Serve it on port "
         "8152 so it keeps running after you finish.",
         checks=[{"type": "http", "url": "http://127.0.0.1:8152/", "contains": "about\\.html"},
                 {"type": "http", "url": "http://127.0.0.1:8152/about.html", "contains": "index\\.html|href=\"/\""}],
         teardown=[{"port": 8152}]),
    task("H39", "multi", "Draw a bar chart of monthly.csv in {dir} as chart.svg (write the SVG yourself, one bar per month, with the "
         "month names as labels).",
         setup_body="put('monthly.csv', 'month,sales\\n' + '\\n'.join(f'{m},{100 + i * 13}' for i, m in enumerate("
                    "['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'])) + '\\n')\n",
         checks=[verify("H39", "s = read('chart.svg')\nif '<svg' not in s: fail('not an SVG')\nbars = len(re.findall(r'<rect', s))\n"
                        "if bars < 12: fail(f'{bars} rects')\nfor m in ('Jan', 'Jun', 'Dec'):\n    if m not in s: fail(f'label {m} missing')\nprint('ok')")]),
    task("H40", "multi", "Analyze sales.csv in {dir} (revenue = units x unit_price) and write report.md with the total revenue, the best "
         "month by revenue, and a table of revenue per category.", setup_body=SALES,
         checks=[verify("H40", SALES_TRUTH + "md = read('report.md').replace(',', '')\ntotal = sum(rev.values())\n"
                        "if f'{total:.2f}' not in md and f'{round(total)}' not in md: fail(f'total {total:.2f} missing')\n"
                        "bym = {}\nfor r in rows: bym[r['date'][5:7]] = bym.get(r['date'][5:7], 0) + int(r['units']) * float(r['unit_price'])\n"
                        "best = max(bym, key=bym.get)\nnames = ['January','February','March','April','May','June','July','August','September','October','November','December']\n"
                        "if names[int(best) - 1] not in md and names[int(best) - 1][:3] not in md and f'2026-{best}' not in md: fail(f'best month {best} missing')\n"
                        "for c in rev:\n    if c not in md: fail(f'category {c} missing')\nprint('ok')")]),
]

if __name__ == "__main__":
    for t in TASKS:
        t.pop("answers", None)
    (HERE / "tasks_hard.json").write_text(json.dumps({"version": 1, "tasks": TASKS}, indent=1), encoding="utf-8")
    print(f"wrote {len(TASKS)} tasks to tasks_hard.json")
