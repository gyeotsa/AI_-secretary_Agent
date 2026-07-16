import os
import sqlite3
import threading
import time
from config import Config
from datetime import datetime
from typing import Optional, Callable, Any, Dict
import json

try:
    import schedule
    SCHEDULE_AVAILABLE = True
except ImportError:
    SCHEDULE_AVAILABLE = False


class AutomationEngine:
    """자동화 엔진: 스케줄 기반으로 LLM 작업 실행"""
    
    def __init__(self):
        self.data_dir = os.path.dirname(Config.DB_PATH)
        self.scheduler_db_path = os.path.join(self.data_dir, "scheduler.db")
        os.makedirs(self.data_dir, exist_ok=True)
        
        self.scheduled_jobs = []
        self.running = False
        self.scheduler_thread = None
        self.stop_event = threading.Event()
        self.job_results = {}  # job_id -> 결과
        
        self._init_db()
        
    def _init_db(self):
        conn = sqlite3.connect(self.scheduler_db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                prompt TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                last_run TEXT,
                last_result TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                run_time TEXT NOT NULL,
                result TEXT,
                error TEXT,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
        """)
        conn.commit()
        conn.close()
        
    def _execute_job(self, job_id: int, prompt: str, description: str):
        """작업 실행 (LLM 호출)"""
        print(f"[Automation] 작업 실행 중: {description} (ID: {job_id})")
        result_text = ""
        error_text = ""
        
        try:
            from core.llm import chat
            from core.memory import build_memory_context
            
            # 메모리 컨텍스트 빌드
            session_id = f"scheduled_job_{job_id}"
            memory_context = build_memory_context(session_id, max_episodes=10, include_semantic=True)
            
            # 시스템 프롬프트 + 메모리 + 작업 프롬프트
            full_prompt = f"{Config.SYSTEM_PROMPT}\n\n"
            if memory_context:
                full_prompt += f"{memory_context}\n\n"
            full_prompt += f"[자동화 작업]\n{prompt}"
            
            # LLM 호출
            result_text = chat(full_prompt)
            print(f"[Automation] 작업 완료: {description}")
            
        except Exception as e:
            error_text = str(e)
            print(f"[Automation] 작업 오류: {e}")
            import traceback
            traceback.print_exc()
            
        finally:
            # 결과 저장
            self._save_job_result(job_id, result_text, error_text)
            
    def _save_job_result(self, job_id: int, result: str, error: str):
        """작업 결과 저장"""
        now = datetime.now().isoformat()
        
        # history 테이블에 기록
        conn = sqlite3.connect(self.scheduler_db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO job_history (job_id, run_time, result, error)
            VALUES (?, ?, ?, ?)
        """, (job_id, now, result, error))
        
        # jobs 테이블 업데이트
        cursor.execute("""
            UPDATE jobs 
            SET last_run = ?, last_result = ?
            WHERE id = ?
        """, (now, result[:1000] if result else error[:1000], job_id))
        
        conn.commit()
        conn.close()
        
        # 메모리에 결과 저장
        self.job_results[job_id] = {
            "run_time": now,
            "result": result,
            "error": error
        }
        
    def _schedule_job(self, job_id: int, description: str, schedule_type: str, schedule_value: str, prompt: str):
        """스케줄 작업 등록"""
        def job():
            self._execute_job(job_id, prompt, description)
            
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
        
    def add_job(self, description: str, schedule_type: str, schedule_value: str, prompt: str) -> str:
        """새 작업 추가"""
        if not SCHEDULE_AVAILABLE:
            return "오류: schedule 라이브러리가 설치되지 않았습니다."
            
        try:
            now = datetime.now().isoformat()
            conn = sqlite3.connect(self.scheduler_db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO jobs (description, schedule_type, schedule_value, prompt, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (description, schedule_type, schedule_value, prompt, now))
            job_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            self._schedule_job(job_id, description, schedule_type, schedule_value, prompt)
            
            return f"✅ 작업이 등록되었습니다 (ID: {job_id}): {description}"
            
        except Exception as e:
            return f"작업 등록 오류: {str(e)}"
            
    def _load_jobs_from_db(self):
        """DB에서 작업 로드"""
        try:
            conn = sqlite3.connect(self.scheduler_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id, description, schedule_type, schedule_value, prompt FROM jobs WHERE enabled = 1")
            rows = cursor.fetchall()
            conn.close()
            
            for row in rows:
                self._schedule_job(row[0], row[1], row[2], row[3], row[4])
                
        except Exception as e:
            print(f"DB에서 작업 로드 오류: {e}")
            
    def list_jobs(self) -> str:
        """작업 목록 보기"""
        try:
            conn = sqlite3.connect(self.scheduler_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id, description, schedule_type, schedule_value, enabled, last_run FROM jobs ORDER BY id DESC")
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                return "등록된 자동화 작업이 없습니다."
                
            result = ["📋 자동화 작업 목록:"]
            for row in rows:
                status = "✅ 활성" if row[4] else "❌ 비활성"
                last_run = row[5] or "아직 실행되지 않음"
                result.append(f"\nID: {row[0]}")
                result.append(f"설명: {row[1]}")
                result.append(f"스케줄: {row[2]} {row[3]}")
                result.append(f"상태: {status}")
                result.append(f"마지막 실행: {last_run}")
                result.append("-" * 40)
                
            return "\n".join(result)
            
        except Exception as e:
            return f"작업 목록 오류: {str(e)}"
            
    def get_job_history(self, job_id: int, limit: int = 10) -> str:
        """작업 실행 기록 보기"""
        try:
            conn = sqlite3.connect(self.scheduler_db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT run_time, result, error 
                FROM job_history 
                WHERE job_id = ? 
                ORDER BY run_time DESC 
                LIMIT ?
            """, (job_id, limit))
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                return f"작업 ID {job_id}의 실행 기록이 없습니다."
                
            result = [f"📊 작업 ID {job_id} 실행 기록:"]
            for row in rows:
                result.append(f"\n시간: {row[0]}")
                if row[1]:
                    result.append(f"결과: {row[1][:200]}..." if len(row[1]) > 200 else f"결과: {row[1]}")
                if row[2]:
                    result.append(f"오류: {row[2]}")
                result.append("-" * 40)
                
            return "\n".join(result)
            
        except Exception as e:
            return f"실행 기록 조회 오류: {str(e)}"
            
    def toggle_job(self, job_id: int, enabled: bool) -> str:
        """작업 활성화/비활성화"""
        try:
            conn = sqlite3.connect(self.scheduler_db_path)
            cursor = conn.cursor()
            cursor.execute("UPDATE jobs SET enabled = ? WHERE id = ?", (1 if enabled else 0, job_id))
            conn.commit()
            conn.close()
            
            if enabled:
                # 스케줄러에 다시 등록
                cursor = sqlite3.connect(self.scheduler_db_path).cursor()
                cursor.execute("SELECT description, schedule_type, schedule_value, prompt FROM jobs WHERE id = ?", (job_id,))
                row = cursor.fetchone()
                if row:
                    self._schedule_job(job_id, row[0], row[1], row[2], row[3])
            else:
                # 스케줄러에서 제거
                schedule.clear(job_id)
                self.scheduled_jobs = [j for j in self.scheduled_jobs if j["id"] != job_id]
                
            status = "활성화" if enabled else "비활성화"
            return f"✅ 작업 ID {job_id}가 {status}되었습니다."
            
        except Exception as e:
            return f"작업 상태 변경 오류: {str(e)}"
            
    def delete_job(self, job_id: int) -> str:
        """작업 삭제"""
        try:
            schedule.clear(job_id)
            
            conn = sqlite3.connect(self.scheduler_db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            cursor.execute("DELETE FROM job_history WHERE job_id = ?", (job_id,))
            conn.commit()
            conn.close()
            
            self.scheduled_jobs = [j for j in self.scheduled_jobs if j["id"] != job_id]
            
            return f"✅ 작업 ID {job_id}가 삭제되었습니다."
            
        except Exception as e:
            return f"작업 삭제 오류: {str(e)}"
            
    def start(self) -> str:
        """자동화 엔진 시작"""
        if not SCHEDULE_AVAILABLE:
            return "오류: schedule 라이브러리가 설치되지 않았습니다."
            
        if self.running:
            return "자동화 엔진이 이미 실행 중입니다."
            
        try:
            self._load_jobs_from_db()
            
            def run_scheduler():
                self.running = True
                while self.running and not self.stop_event.is_set():
                    schedule.run_pending()
                    time.sleep(1)
                    
            self.scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
            self.scheduler_thread.start()
            
            return "✅ 자동화 엔진이 시작되었습니다."
            
        except Exception as e:
            return f"자동화 엔진 시작 오류: {str(e)}"
            
    def stop(self) -> str:
        """자동화 엔진 중지"""
        if not self.running:
            return "자동화 엔진이 실행 중이 아닙니다."
            
        self.running = False
        self.stop_event.set()
        
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
            
        return "✅ 자동화 엔진이 중지되었습니다."
        
    def is_running(self) -> bool:
        """실행 중 여부"""
        return self.running


# 기존 SchedulerManager 유지 (하위 호환성)
class SchedulerManager:
    def __init__(self):
        self.engine = AutomationEngine()
        
    def init_db(self):
        pass
        
    def add_job(self, description: str, schedule_type: str, schedule_value: str, prompt: str) -> str:
        return self.engine.add_job(description, schedule_type, schedule_value, prompt)
        
    def list_jobs(self) -> str:
        return self.engine.list_jobs()
        
    def delete_job(self, job_id: int) -> str:
        return self.engine.delete_job(job_id)
        
    def start_scheduler(self) -> str:
        return self.engine.start()
        
    def stop_scheduler(self) -> str:
        return self.engine.stop()


# 싱글톤 인스턴스
_automation_engine = None
_scheduler_manager = None


def get_automation_engine() -> AutomationEngine:
    global _automation_engine
    if _automation_engine is None:
        _automation_engine = AutomationEngine()
    return _automation_engine


def get_scheduler_manager() -> SchedulerManager:
    global _scheduler_manager
    if _scheduler_manager is None:
        _scheduler_manager = SchedulerManager()
    return _scheduler_manager
