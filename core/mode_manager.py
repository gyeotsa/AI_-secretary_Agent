import os
import time
import subprocess
import pygetwindow as gw
from typing import Dict, List, Optional
from config import Config

class ModeAppConfig:
    def __init__(self, name: str, path: str, delay: float = 2.0, patterns: Optional[List[str]] = None):
        self.name = name
        self.path = path
        self.delay = delay
        self.patterns = patterns or [name]

class ModeManager:
    def __init__(self):
        self.work_mode_apps = self._init_work_mode_apps()
        self.game_mode_apps = self._init_game_mode_apps()
    
    def _init_work_mode_apps(self) -> Dict[str, ModeAppConfig]:
        # 사용자의 환경에 맞게 수정하세요!
        return {
            # "notepad": ModeAppConfig("Notepad", "notepad.exe", 1.0, ["Notepad", "메모장"]),
            # "chrome": ModeAppConfig("Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe", 2.0, ["Chrome", "구글 Chrome"]),
        }
    
    def _init_game_mode_apps(self) -> Dict[str, ModeAppConfig]:
        # 사용자의 환경에 맞게 수정하세요!
        return {
            # "steam": ModeAppConfig("Steam", r"C:\Program Files (x86)\Steam\Steam.exe", 3.0, ["Steam", "스팀"]),
        }
    
    def _get_snapshot(self) -> List[str]:
        try:
            return [win.title for win in gw.getAllWindows()]
        except:
            return []
    
    def _find_new_window(self, patterns: List[str], before: List[str]) -> Optional:
        after = self._get_snapshot()
        for win in gw.getAllWindows():
            if win.title in before:
                continue
            for pattern in patterns:
                if pattern.lower() in win.title.lower():
                    return win
        return None
    
    def _arrange_window(self, win, layout: tuple):
        try:
            x, y, w, h = layout
            win.moveTo(x, y)
            win.resizeTo(w, h)
            return True
        except Exception as e:
            print(f"윈도우 배치 오류: {e}")
            return False
    
    def activate_work_mode(self):
        print("업무 모드 활성화 중, 보스.")
        for app_name, cfg in self.work_mode_apps.items():
            try:
                before = self._get_snapshot()
                subprocess.Popen(cfg.path)
                time.sleep(cfg.delay)
                new_win = self._find_new_window(cfg.patterns, before)
                if new_win:
                    print(f"{app_name} 실행 완료, 보스.")
            except Exception as e:
                print(f"{app_name} 실행 오류: {e}")
        return "업무 환경이 준비되었습니다, 보스."
    
    def activate_game_mode(self):
        print("게임 모드 활성화 중, 보스.")
        for app_name, cfg in self.game_mode_apps.items():
            try:
                before = self._get_snapshot()
                subprocess.Popen(cfg.path)
                time.sleep(cfg.delay)
                new_win = self._find_new_window(cfg.patterns, before)
                if new_win:
                    print(f"{app_name} 실행 완료, 보스.")
            except Exception as e:
                print(f"{app_name} 실행 오류: {e}")
        return "게임 환경이 준비되었습니다, 보스."
