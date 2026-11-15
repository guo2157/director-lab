"""临时脚本：通过 CDP 截取本机 Director Lab 页面，用于制作闲鱼素材图。"""
import base64
import json
import os
import subprocess
import time
import urllib.request

import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ROOT = r"E:\project\director-lab"
OUT = os.path.join(ROOT, "sales", "xianyu", "raw")
PORT = 9333
PROFILE = os.path.join(OUT, "cdp_profile")

PAGES = [
    ("storyboard", "http://localhost:5173/project/prj_257406918e63/storyboard"),
    ("storyboard_images", "http://localhost:5173/project/prj_257406918e63/storyboard-images"),
    ("assets", "http://localhost:5173/project/prj_257406918e63/assets"),
    ("script", "http://localhost:5173/project/prj_257406918e63/script"),
    ("series", "http://localhost:5173/series/srs_2bc922bfba66"),
]


def launch():
    args = [
        CHROME,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1600,1000",
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ws_url():
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=1) as r:
                for t in json.load(r):
                    if t.get("type") == "page":
                        return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("CDP 未就绪")


def main():
    proc = launch()
    try:
        ws = websocket.create_connection(ws_url(), timeout=30)
        msg_id = 0

        def send(method, params=None):
            nonlocal msg_id
            msg_id += 1
            ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            while True:
                data = json.loads(ws.recv())
                if data.get("id") == msg_id:
                    return data.get("result", {})

        send("Page.enable")
        send("Emulation.setDeviceMetricsOverride", {
            "width": 1440, "height": 900, "deviceScaleFactor": 2, "mobile": False,
        })
        for name, url in PAGES:
            send("Page.navigate", {"url": url})
            time.sleep(8)
            shot = send("Page.captureScreenshot", {"format": "png"})
            path = os.path.join(OUT, f"{name}.png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            print(name, os.path.getsize(path))
        ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
