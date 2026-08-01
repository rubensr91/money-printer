import subprocess, sys, os, time

bot_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "telegram_bot.py")
python_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "Scripts", "python.exe")
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_output.log")

with open(log_file, "w", encoding="utf-8") as f:
    proc = subprocess.Popen(
        [python_exe, bot_script],
        stdout=f,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    time.sleep(3)
    if proc.poll() is None:
        print("BOT_STARTED_OK")
        print(f"PID: {proc.pid}")
    else:
        print(f"BOT_FAILED exit_code={proc.returncode}")
        with open(log_file) as lf:
            print(lf.read())
