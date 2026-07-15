
import sys

print("pyttsx3 테스트 시작...")

try:
    import pyttsx3
    print("pyttsx3 import 성공!")
    
    engine = pyttsx3.init()
    print("Engine 초기화 성공!")
    
    voices = engine.getProperty('voices')
    print(f"사용 가능한 음성 개수: {len(voices)}")
    for i, voice in enumerate(voices):
        print(f"  음성 {i}: {voice.name}, 언어: {voice.languages}")
    
    # 한국어 음성 설정
    korean_voice_found = False
    for voice in voices:
        if 'ko' in str(voice.languages).lower() or 'korean' in voice.name.lower():
            engine.setProperty('voice', voice.id)
            print(f"한국어 음성 설정: {voice.name}")
            korean_voice_found = True
            break
    
    if not korean_voice_found:
        print("한국어 음성을 찾을 수 없어 기본 음성을 사용합니다.")
    
    # 테스트 문장
    test_text = "안녕하세요, 보스! 자비스 테스트입니다."
    print(f"말할 내용: {test_text}")
    
    engine.say(test_text)
    engine.runAndWait()
    print("음성 재생 완료!")
    
except Exception as e:
    print(f"오류 발생: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

