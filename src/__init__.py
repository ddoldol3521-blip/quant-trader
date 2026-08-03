"""퀀트트레이더 내부 모듈 모음.

이 파일이 비어 있어도 반드시 있어야 한다. 없으면 src가 namespace package가
되는데, Streamlit이 실행할 때마다 sys.modules를 정리하는 과정에서 부모 경로를
잃고 import가 KeyError로 터진다 (Streamlit Cloud 배포에서 실제로 겪었다).
"""
