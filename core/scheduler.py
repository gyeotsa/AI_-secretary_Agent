import os
import sqlite3
import threading
import time
from config import Config
from datetime import datetime, timedelta
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
        self.scheduler = schedule.Scheduler() if SCHEDULE_AVAILABLE else None
        self.running = False
        self.scheduler_thread = None
        self.stop_event = threading.Event()
        self.job_results = {}  # job_id -> 결과
        self.result_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self._running_jobs = set()
        self._job_lock = threading.Lock()
        self._last_tick_monotonic = time.monotonic()
        self._restored_jobs = 0
        
        self._init_db()

    def set_result_callback(self, callback: Optional[Callable[[Dict[str, Any]], None]]):
        """예약 작업 완료 결과를 GUI 등 외부 소비자에게 전달한다."""
        self.result_callback = callback
        
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
                action_type TEXT NOT NULL DEFAULT 'llm',
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""CREATE TABLE IF NOT EXISTS scheduler_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(jobs)").fetchall()}
        if "action_type" not in columns:
            cursor.execute("ALTER TABLE jobs ADD COLUMN action_type TEXT NOT NULL DEFAULT 'llm'")
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
        
    def _execute_job(self, job_id: int, prompt: str, description: str,
                     action_type: str = "llm"):
        """작업 실행 (LLM 호출)"""
        with self._job_lock:
            if job_id in self._running_jobs:
                print(f"[Automation] 중복 실행 억제: {job_id}")
                return
            self._running_jobs.add(job_id)
        print(f"[Automation] 작업 실행 중: {description} (ID: {job_id})")
        result_text = ""
        error_text = ""
        
        try:
            if action_type == "alarm":
                result_text = prompt or "알람 시간입니다."
                print(f"[Automation] 알람 발생: {result_text}")
                return
            from core.llm import get_llm_client
            from core.memory import build_memory_context
            
            # 메모리 컨텍스트 빌드
            session_id = f"scheduled_job_{job_id}"
            memory_context = build_memory_context(session_id, max_episodes=10, include_semantic=True)
            
            # 시스템 프롬프트 + 메모리 + 작업 프롬프트
            full_prompt = f"{Config.get_system_prompt()}\n\n"
            if memory_context:
                full_prompt += f"{memory_context}\n\n"
            full_prompt += f"[자동화 작업]\n{prompt}"
            
            # LLM 호출
            result_text = get_llm_client().chat([{"role": "user", "content": full_prompt}])
            print(f"[Automation] 작업 완료: {description}")
            
        except Exception as e:
            error_text = str(e)
            print(f"[Automation] 작업 오류: {e}")
            import traceback
            traceback.print_exc()
            
        finally:
            # 결과 저장
            self._save_job_result(job_id, result_text, error_text, action_type, description)
            with self._job_lock:
                self._running_jobs.discard(job_id)
            
    def _save_job_result(self, job_id: int, result: str, error: str,
                         action_type: str = "llm", description: str = ""):
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
            SET last_run = ?, last_result = ?, enabled = CASE WHEN ? = 'alarm' THEN 0 ELSE enabled END
            WHERE id = ?
        """, (now, result[:1000] if result else error[:1000], action_type, job_id))
        
        conn.commit()
        conn.close()
        
        # 메모리에 결과 저장
        self.job_results[job_id] = {
            "run_time": now,
            "result": result,
            "error": error
        }
        if self.result_callback:
            try:
                self.result_callback({
                    "job_id": job_id,
                    "run_time": now,
                    "result": result,
                    "error": error,
                    "action_type": action_type,
                    "description": description,
                })
            except Exception as exc:
                print(f"[Automation] 결과 callback 오류: {exc}")
        
    def _schedule_job(self, job_id: int, description: str, schedule_type: str,
                      schedule_value: str, prompt: str, action_type: str = "llm"):
        """스케줄 작업 등록"""
        def job():
            self._execute_job(job_id, prompt, description, action_type)
            if schedule_type == "once_at":
                return schedule.CancelJob
            
        if schedule_type == "every_minutes":
            self.scheduler.every(int(schedule_value)).minutes.do(job).tag(job_id)
        elif schedule_type == "every_hours":
            self.scheduler.every(int(schedule_value)).hours.do(job).tag(job_id)
        elif schedule_type == "every_days":
            self.scheduler.every(int(schedule_value)).days.do(job).tag(job_id)
        elif schedule_type == "daily_at":
            self.scheduler.every().day.at(schedule_value).do(job).tag(job_id)
        elif schedule_type == "every_weeks":
            self.scheduler.every(int(schedule_value)).weeks.do(job).tag(job_id)
        elif schedule_type == "once_at":
            target = datetime.fromisoformat(schedule_value)
            now = datetime.now(target.tzinfo) if target.tzinfo else datetime.now()
            delay_seconds = max(0.1, (target - now).total_seconds())
            self.scheduler.every(delay_seconds).seconds.do(job).tag(job_id)
        else:
            raise ValueError(f"지원하지 않는 schedule_type입니다: {schedule_type}")
            
        self.scheduled_jobs.append({
            "id": job_id,
            "description": description,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "prompt": prompt,
            "action_type": action_type,
        })
        
    def add_job(self, description: str, schedule_type: str, schedule_value: str, prompt: str) -> str:
        """새 작업 추가"""
        if not SCHEDULE_AVAILABLE:
            return "오류: schedule 라이브러리가 설치되지 않았습니다."
        valid_types = {"every_minutes", "every_hours", "every_days", "daily_at", "every_weeks"}
        if schedule_type not in valid_types:
            return f"작업 등록 오류: 지원하지 않는 schedule_type입니다: {schedule_type}"
        if schedule_type != "daily_at":
            try:
                if int(schedule_value) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return "작업 등록 오류: 반복 간격은 1 이상의 정수여야 합니다."
            
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
            
            return f"작업이 등록되었습니다 (ID: {job_id}): {description}"
            
        except Exception as e:
            return f"작업 등록 오류: {str(e)}"

    def add_alarm(self, delay_seconds: int, message: str = "알람 시간입니다.") -> str:
        """한 번만 실행되는 상대 시간 알람을 등록하고 엔진을 보장해 시작한다."""
        try:
            delay_seconds = int(delay_seconds)
            if delay_seconds <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return "알람 등록 오류: 알람 시간은 1초 이상이어야 합니다."
        if not SCHEDULE_AVAILABLE:
            return "오류: schedule 라이브러리가 설치되지 않았습니다."
        if not self.running:
            started = self.start()
            if started.startswith("오류:"):
                return started
        target = datetime.now().astimezone() + timedelta(seconds=delay_seconds)
        description = f"{delay_seconds}초 뒤 알람"
        try:
            with sqlite3.connect(self.scheduler_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO jobs
                    (description, schedule_type, schedule_value, prompt, action_type, created_at)
                    VALUES (?, 'once_at', ?, ?, 'alarm', ?)
                """, (description, target.isoformat(), message, datetime.now().isoformat()))
                job_id = cursor.lastrowid
            self._schedule_job(
                job_id, description, "once_at", target.isoformat(), message, "alarm"
            )
            return json.dumps({
                "status": "scheduled", "job_id": job_id, "delay_seconds": delay_seconds,
                "fire_at": target.isoformat(), "message": message,
            }, ensure_ascii=False)
        except Exception as exc:
            return f"알람 등록 오류: {exc}"
            
    def _load_jobs_from_db(self):
        """DB에서 작업 로드"""
        try:
            self.scheduler.clear()
            self.scheduled_jobs.clear()
            conn = sqlite3.connect(self.scheduler_db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, description, schedule_type, schedule_value, prompt, action_type "
                "FROM jobs WHERE enabled = 1"
            )
            rows = cursor.fetchall()
            conn.close()
            
            for row in rows:
                self._schedule_job(row[0], row[1], row[2], row[3], row[4], row[5])
            self._restored_jobs = len(rows)
            self._write_state("last_restore", json.dumps({"count": len(rows), "at": datetime.now().astimezone().isoformat()}))
                
        except Exception as e:
            print(f"DB에서 작업 로드 오류: {e}")

    def _write_state(self, key: str, value: str):
        with sqlite3.connect(self.scheduler_db_path) as conn:
            conn.execute("INSERT INTO scheduler_state(key,value,updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                         (key, value, datetime.now().astimezone().isoformat()))

    def runtime_diagnostics(self) -> dict:
        thread_alive = bool(self.scheduler_thread and self.scheduler_thread.is_alive())
        with sqlite3.connect(self.scheduler_db_path) as conn:
            state = {row[0]: {"value": row[1], "updated_at": row[2]} for row in
                     conn.execute("SELECT key,value,updated_at FROM scheduler_state").fetchall()}
        return {"running": self.running, "thread_alive": thread_alive,
                "scheduled_jobs": len(self.scheduled_jobs), "restored_jobs": self._restored_jobs,
                "running_jobs": len(self._running_jobs), "state": state}

    def soak_test(self, iterations: int = 1000) -> dict:
        """Accelerated scheduler loop test without waiting wall-clock hours."""
        iterations = max(1, min(int(iterations), 100_000))
        failures = []
        started = time.monotonic()
        for index in range(iterations):
            try:
                self.scheduler.run_pending()
            except Exception as exc:
                failures.append({"iteration": index, "error": str(exc)})
                break
        return {"iterations": iterations, "failures": failures,
                "duration_ms": (time.monotonic() - started) * 1000,
                "scheduled_jobs": len(self.scheduler.jobs) if self.scheduler else 0}
            
    def list_jobs(self) -> str:
        """작업 목록 보기"""
        try:
            rows = self.get_job_records()
            
            if not rows:
                return "등록된 자동화 작업이 없습니다."
                
            result = ["자동화 작업 목록:"]
            for row in rows:
                status = "활성" if row["enabled"] else "비활성"
                last_run = row["last_run"] or "아직 실행되지 않음"
                result.append(f"\nID: {row['id']}")
                result.append(f"설명: {row['description']}")
                result.append(f"스케줄: {row['schedule_type']} {row['schedule_value']}")
                result.append(f"상태: {status}")
                result.append(f"마지막 실행: {last_run}")
                result.append("-" * 40)
                
            return "\n".join(result)
            
        except Exception as e:
            return f"작업 목록 오류: {str(e)}"

    def get_job_records(self) -> list[dict]:
        """도구 계층이 메시지 파싱 없이 검증할 수 있는 작업 레코드를 반환한다."""
        with sqlite3.connect(self.scheduler_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, description, schedule_type, schedule_value, prompt, "
                "action_type, enabled, last_run, created_at FROM jobs ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job_record(self, job_id: int) -> Optional[dict]:
        with sqlite3.connect(self.scheduler_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, description, schedule_type, schedule_value, prompt, "
                "action_type, enabled, last_run, created_at FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None
            
    def get_job_history(self, job_id: int, limit: int = 10) -> str:
        """작업 실행 기록 보기"""
        try:
            rows = self.get_job_history_records(job_id, limit)
            
            if not rows:
                return f"작업 ID {job_id}의 실행 기록이 없습니다."
                
            result = [f"작업 ID {job_id} 실행 기록:"]
            for row in rows:
                result.append(f"\n시간: {row['run_time']}")
                if row["result"]:
                    value = row["result"]
                    result.append(f"결과: {value[:200]}..." if len(value) > 200 else f"결과: {value}")
                if row["error"]:
                    result.append(f"오류: {row['error']}")
                result.append("-" * 40)
                
            return "\n".join(result)
            
        except Exception as e:
            return f"실행 기록 조회 오류: {str(e)}"

    def get_job_history_records(self, job_id: int, limit: int = 10) -> list[dict]:
        with sqlite3.connect(self.scheduler_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT run_time, result, error FROM job_history "
                "WHERE job_id = ? ORDER BY run_time DESC LIMIT ?",
                (job_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]
            
    def toggle_job(self, job_id: int, enabled: bool) -> str:
        """작업 활성화/비활성화"""
        try:
            if self.get_job_record(job_id) is None:
                return f"작업 상태 변경 오류: 작업 ID {job_id}를 찾을 수 없습니다."
            conn = sqlite3.connect(self.scheduler_db_path)
            cursor = conn.cursor()
            cursor.execute("UPDATE jobs SET enabled = ? WHERE id = ?", (1 if enabled else 0, job_id))
            conn.commit()
            conn.close()
            
            if enabled:
                # 스케줄러에 다시 등록
                with sqlite3.connect(self.scheduler_db_path) as lookup_conn:
                    cursor = lookup_conn.cursor()
                    cursor.execute("SELECT description, schedule_type, schedule_value, prompt FROM jobs WHERE id = ?", (job_id,))
                    row = cursor.fetchone()
                if row:
                    self._schedule_job(job_id, row[0], row[1], row[2], row[3])
            else:
                # 스케줄러에서 제거
                self.scheduler.clear(job_id)
                self.scheduled_jobs = [j for j in self.scheduled_jobs if j["id"] != job_id]
                
            status = "활성화" if enabled else "비활성화"
            return f"작업 ID {job_id}가 {status}되었습니다."
            
        except Exception as e:
            return f"작업 상태 변경 오류: {str(e)}"
            
    def delete_job(self, job_id: int) -> str:
        """작업 삭제"""
        try:
            if self.get_job_record(job_id) is None:
                return f"작업 삭제 오류: 작업 ID {job_id}를 찾을 수 없습니다."
            self.scheduler.clear(job_id)
            
            conn = sqlite3.connect(self.scheduler_db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            cursor.execute("DELETE FROM job_history WHERE job_id = ?", (job_id,))
            conn.commit()
            conn.close()
            
            self.scheduled_jobs = [j for j in self.scheduled_jobs if j["id"] != job_id]
            
            return f"작업 ID {job_id}가 삭제되었습니다."
            
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
            self.stop_event.clear()
            self.running = True
            self._last_tick_monotonic = time.monotonic()
            
            def run_scheduler():
                while self.running and not self.stop_event.is_set():
                    now = time.monotonic()
                    if now - self._last_tick_monotonic > 10:
                        print("[Automation] 절전·장시간 중단 감지: DB 작업 복원")
                        self._load_jobs_from_db()
                    self._last_tick_monotonic = now
                    try:
                        self.scheduler.run_pending()
                        self._write_state("heartbeat", json.dumps({
                            "at": datetime.now().astimezone().isoformat(),
                            "jobs": len(self.scheduled_jobs),
                        }))
                    except Exception as exc:
                        print(f"[Automation] scheduler tick 오류: {exc}")
                    time.sleep(1)
                    
            self.scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
            self.scheduler_thread.start()
            
            return "자동화 엔진이 시작되었습니다."
            
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
            
        return "자동화 엔진이 중지되었습니다."
        
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
