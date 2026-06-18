#!/usr/bin/env python3
"""Local server for the UFC dashboard with an on-demand Refresh button.

Serves the generated index.html and injects a floating "Refresh odds" button.
Pressing it runs refresh.py (re-pulls Pinnacle + Kalshi), commits, and pushes to
GitHub Pages so the shared live page updates too, then reloads. The launchd
auto-refresh still runs independently; this just adds manual on-demand refreshing.

Run:
    python3 server.py        # then open http://127.0.0.1:5056
"""
import os
import subprocess
from flask import Flask, Response

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
app = Flask(__name__)

WIDGET = """
<style>
 #rbtn{position:fixed;top:16px;right:18px;z-index:9999;background:#d20a0a;color:#fff;
   border:none;border-radius:8px;padding:9px 16px;font-size:13px;font-weight:600;
   cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.4)}
 #rbtn:disabled{opacity:.7;cursor:default}
</style>
<button id="rbtn" onclick="doRefresh()">&#x21bb; Refresh odds</button>
<script>
async function doRefresh(){
  const b=document.getElementById('rbtn');
  b.disabled=true; b.textContent='\\u23F3 Refreshing\\u2026 (~1 min)';
  try{
    const r=await fetch('/refresh',{method:'POST'});
    if(r.ok){ b.textContent='\\u2713 Done \\u2014 reloading'; location.reload(); }
    else { b.textContent='\\u26A0 Failed'; b.disabled=false; }
  }catch(e){ b.textContent='\\u26A0 Error'; b.disabled=false; }
}
</script>
"""


@app.route("/")
def index():
    try:
        html = open(INDEX, encoding="utf-8").read()
    except FileNotFoundError:
        return "No index.html yet — run python3 refresh.py first.", 404
    return Response(html.replace("</body>", WIDGET + "</body>"), mimetype="text/html")


@app.route("/refresh", methods=["POST"])
def refresh():
    try:
        res = subprocess.run(["python3", os.path.join(HERE, "refresh.py")],
                             cwd=HERE, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "refresh timed out", 500
    if res.returncode != 0:
        return ((res.stderr or "")[-500:], 500)
    # publish to GitHub Pages (best-effort) so the shared live page updates too
    try:
        if subprocess.run(["git", "-C", HERE, "status", "--porcelain", "index.html"],
                          capture_output=True, text=True).stdout.strip():
            subprocess.run(["git", "-C", HERE, "add", "index.html"], capture_output=True)
            subprocess.run(["git", "-C", HERE, "-c", "user.name=gehrenberg823",
                            "-c", "user.email=gehrenberg@awesemo.com", "commit",
                            "-m", "Refresh odds (manual)"], capture_output=True)
            subprocess.run(["git", "-C", HERE, "push", "-q", "origin", "main"], capture_output=True)
    except Exception:
        pass   # local refresh succeeded even if the push didn't
    return ("ok", 200)


if __name__ == "__main__":
    print("UFC dashboard -> http://127.0.0.1:5056")
    app.run(port=5056, threaded=True)
