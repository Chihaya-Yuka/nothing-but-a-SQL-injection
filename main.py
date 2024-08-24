from collections import defaultdict
import fastapi
import time
import threading
import sqlite3
import os
import re
from fastapi import Request
from fastapi.responses import JSONResponse

import fastapi.middleware.cors

app = fastapi.FastAPI()

app.add_middleware(
    fastapi.middleware.cors.CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def detect_sql_injection(text):
    sql_patterns = [
        r"(\bOR\b|\bAND\b).*?\b\d+\b.*?(\b=|LIKE|IN)\b",
        r"(\bUNION\b\s+\bALL\b\s+\bSELECT\b)",
        r"(\bSELECT\b\s+\w+\s+FROM\b\s+\w+)",
        r"(\bDROP\b\s+\bTABLE\b\s+\bIF\b\s+\bEXISTS\b)",
        r"(\bINSERT\b\s+INTO\b\s+\w+\s+\bVALUES\b)",
        r"(\bUPDATE\b\s+\w+\s+\bSET\b\s+\w+\s*=\s*.*?\bWHERE\b)",
    ]
    for pattern in sql_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True # SQL Injection detected
    return False # No SQL Injection detected

def predict_success(text):
    success_patterns = [
        r"(\bUNION\b\s+\bALL\b\s+\bSELECT\b)",
        r"(\bDROP\b\s+\bTABLE\b\s+\bIF\b\s+\bEXISTS\b)",
        r"(\bINSERT\b\s+INTO\b\s+\w+\s+\bVALUES\b)",
    ]
    for pattern in success_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True # High likelihood of success
    return False # Low likelihood of success

def analyze_sql_injection(text):
    detection_result = detect_sql_injection(text)
    success_prediction = predict_success(text)
    return detection_result, success_prediction

class Counter:
    def __init__(self, db_file="count.db") -> None:
        self.db_file: str = db_file
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self.conn.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, count INTEGER)''')
        self.conn.commit()
        self._migrate_from_file()
    
    def _migrate_from_file(self) -> None:
        if os.path.exists("count.txt"):
            with open("count.txt", "r") as f:
                for line in f:
                    username, count = line.strip().split(":")
                    self.conn.execute("INSERT INTO users (username, count) VALUES (?, ?)", (username, int(count)))
                self.conn.commit()
            os.remove("count.txt")
    
    def _sync_to_db(self) -> None:
        self.conn.commit()

    def _sync_thread(self, interval: int = 5) -> None:
        while True:
            self._sync_to_db()
            time.sleep(interval)
    
    def start_sync(self, interval: int = 5) -> None:
        threading.Thread(target=self._sync_thread, args=(interval,)).start()
    
    def increase(self, username: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO users (username, count) VALUES (?, 1) ON CONFLICT(username) DO UPDATE SET count=count+1", (username,))
        self.conn.commit()
        cursor.execute("SELECT count FROM users WHERE username=?", (username,))
        return cursor.fetchone()[0]

    def get_rank(self, username: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute("SELECT username, count FROM users ORDER BY count DESC")
        sorted_users = cursor.fetchall()
        for rank, (user, _) in enumerate(sorted_users, start=1):
            if user == username:
                return rank if rank <= 10 else "10+"
        return -1

    RATE_LIMIT = 50
    RATE_LIMIT_INTERVAL = 1
    rate_limiter = defaultdict(lambda: {"count": 0, "last_reset": time.time()})

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        client_ip = request.client.host
        now = time.time()
        if now - Counter.rate_limiter[client_ip]["last_reset"] > Counter.RATE_LIMIT_INTERVAL:
            Counter.rate_limiter[client_ip] = {"count": 1, "last_reset": now}
        else:
            Counter.rate_limiter[client_ip]["count"] += 1
            if Counter.rate_limiter[client_ip]["count"] > Counter.RATE_LIMIT:
                return JSONResponse(status_code=429, content={"status": 429, "description": "Too Many Requests"})
        response = await call_next(request)
        return response

counter = Counter()
counter.start_sync()

@app.get("/")
async def root(username: str = None, text: str = None):
    if username:
        if len(username) > 20 or not re.match(r"^[a-zA-Z0-9_]+$", username):
            return {"status": 400, "description": "Bad Request: Username can only contain letters, numbers, and underscores, and must be 20 characters or less."}
        count = counter.increase(username)
        rank = counter.get_rank(username)
        return {"status": 200, "description": f"User {username} clicked {count} times.", "count": count, "rank": rank}
    
    if text:
        detection_result, success_prediction = analyze_sql_injection(text)
        if detection_result:
            counter.increase("sql_injection_detected")
        return {
            "status": 200,
            "description": detection_result or "No SQL Injection detected",
            "success_prediction": success_prediction
        }
    
    cursor = counter.conn.cursor()
    cursor.execute("SELECT username, count FROM users ORDER BY count DESC LIMIT 10")
    sorted_users = cursor.fetchall()
    top_users = [{"username": user, "clicks": clicks, "rank": rank + 1} for rank, (user, clicks) in enumerate(sorted_users)]
    cursor.execute("SELECT SUM(count) FROM users")
    total_clicks = cursor.fetchone()[0] or 0
    return {"status": 200, "description": "Top 10 users and total clicks", "top_users": top_users, "total_clicks": total_clicks}
