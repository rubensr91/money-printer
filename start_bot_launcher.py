import subprocess, sys, os, time

root = os.path.dirname(os.path.abspath(__file__))
bot_script = os.path.join(root, "src", "telegram_bot.py")
web_script = os.path.join(root, "src", "web_ui.py")
python_exe = os.path.join(root, "venv", "Scripts", "python.exe")
log_file = os.path.join(root, "bot_output.log")

# Start Web UI (dashboard at http://127.0.0.1:5050)
web_proc = subprocess.Popen(
    [python_exe, web_script],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    cwd=root,
)

# Start bot
with open(log_file, "w", encoding="utf-8") as f:
    proc = subprocess.Popen(
        [python_exe, bot_script],
        stdout=f,
        stderr=subprocess.STDOUT,
        cwd=root,
    )
    time.sleep(3)
    if proc.poll() is None:
        print("BOT_STARTED_OK")
        print(f"PID: {proc.pid}")
    else:
        print(f"BOT_FAILED exit_code={proc.returncode}")
        with open(log_file) as lf:
            print(lf.read())

print("WEB_UI_STARTED (http://127.0.0.1:5050)")
