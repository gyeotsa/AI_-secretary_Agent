import os
import sqlite3
import threading
import time
from config import Config
from datetime import datetime


try:
    import schedule
    SCHEDULE_AVAILABLE = True
except ImportError:
    SCHEDULE_AVAILABLE = False


class SchedulerManager:
    def __init__(self):
        self.data_dir = os.path.dirname(Config.DB_PATH)
        self.scheduler_db_path = os.path.join(self.data_dir, "scheduler.db")
        os.makedirs(self.data_dir, exist_ok=True)
        
        self.scheduled_jobs = []
        self.running = False
        self.scheduler_thread = None
        
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.scheduler_db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT,
                schedule_type TEXT,
                schedule_value TEXT,
                prompt TEXT,
                created_at TEXT
            )
        """)
        conn.commit()
        conn.close()

    def add_job(self, description: str, schedule_type: str, schedule_value: str, prompt: str) -> str:
        if not SCHEDULE_AVAILABLE:
            return "오류: schedule가 설치되지 않았습니다. requirements.txt를 확인하세요."
        
        try:
            conn = sqlite3.connect(self.scheduler_db_path)
            conn.execute(
                "INSERT INTO jobs (description, schedule_type, schedule_value, prompt, created_at) VALUES (?, ?, ?, ?, ?)",
                (description, schedule_type, schedule_value, prompt, datetime.now().isoformat())
            )
            conn.commit()
            job_id = conn.lastrowid
            conn.close()
            
            self._schedule_job(job_id, description, schedule_type, schedule_value, prompt)
            
            return f"✅ 작업 '{description}'가 성공적으로 등록되었습니다! (ID: {job_id})"
        
        except Exception as e:
            return f"작업 등록 오류: {str(e)}"

    def _schedule_job(self, job_id: int, description: str, schedule_type: str, schedule_value: str, prompt: str):
        def job():
            print(f"\n⏰ 스케줄 실행: {description}")
            print(f"   프롬프트: {prompt}")
        
        if schedule_type == "every_minutes":
            schedule.every(int(schedule_value)).minutes.do(job).tag(job_id)
        elif schedule_type == "every_hours":
            schedule.every(int(schedule_value)).hours.do(job).tag(job_id)
        elif schedule_type == "every_days":
            schedule.every(int(schedule_value)).days.do(job).tag(job_id)
        elif schedule_type == "daily_at":
            schedule.every().day.at(schedule_value).do(job).tag(job_id)
        elif schedule_type == "every_weeks":
            schedule.every(int(schedule_value)).weeks.do(job).tag(job_id)
        
        self.scheduled_jobs.append({
            "id": job_id,
            "description": description,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "prompt": prompt
        })

    def list_jobs(self) -> str:
        try:
            conn = sqlite3.connect(self.scheduler_db_path)
            rows = conn.execute("SELECT id, description, schedule_type, schedule_value, prompt FROM jobs").fetchall()
            conn.close()
            
            if not rows:
                return "등록된 스케줄 작업이 없습니다."
            
            result = ["📋 스케줄 작업 목록:"]
            for row in rows:
                result.append(f"\nID: {row[0]}")
                result.append(f"설명: {row[1]}")
                result.append(f"스케줄: {row[2]} {row[3]}")
                result.append(f"프롬프트: {row[4]}")
                result.append("-" * 40)
            
            return "\n".join(result)
        
        except Exception as e:
            return f"작업 목록 오류: {str(e)}"

    def delete_job(self, job_id: int) -> str:
        try:
            schedule.clear(job_id)
            
            conn = sqlite3.connect(self.scheduler_db_path)
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            conn.close()
            
            self.scheduled_jobs = [job for job in self.scheduled_jobs if job["id"] != job_id]
            
            return f"✅ 작업 ID {job_id}가 성공적으로 삭제되었습니다!"
        
        except Exception as e:
            return f"작업 삭제 오류: {str(e)}"

    def start_scheduler(self) -> str:
        if not SCHEDULE_AVAILABLE:
            return "오류: schedule가 설치되지 않았습니다. requirements.txt를 확인하세요."
        
        if self.running:
            return "스케줄러가 이미 실행 중입니다."
        
        try:
            self._load_jobs_from_db()
            
            def run_scheduler():
                self.running = True
                while self.running:
                    schedule.run_pending()
                    time.sleep(1)
            
            self.scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
            self.scheduler_thread.start()
            
            return "✅ 스케줄러가 성공적으로 시작되었습니다!"
        
        except Exception as e:
            return f"스케줄러 시작 오류: {str(e)}"

    def stop_scheduler(self) -> str:
        if not self.running:
            return "스케줄러가 실행 중이지 않습니다."
        
        self.running = False
        return "✅ 스케줄러가 성공적으로 중지되었습니다!"

    def _load_jobs_from_db(self):
        try:
            conn = sqlite3.connect(self.scheduler_db_path)
            rows = conn.execute("SELECT id, description, schedule_type, schedule_value, prompt FROM jobs").fetchall()
            conn.close()
            
            for row in rows:
                self._schedule_job(row[0], row[1], row[2], row[3], row[4])
        
        except Exception as e:
            print(f"DB에서 작업 로드 오류: {str(e)}")


# Singleton instance
_scheduler_manager = None


def get_scheduler_manager() -> SchedulerManager:
    global _scheduler_manager
    if _scheduler_manager is None:
        _scheduler_manager = SchedulerManager()
    return _scheduler_manager
