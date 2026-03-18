import threading

def post_fork(server, worker):
    """在每個 worker fork 後啟動背景輪詢 thread"""
    from app import poll_loop
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    print(f"[gunicorn] poll_loop thread started in worker {worker.pid}")
