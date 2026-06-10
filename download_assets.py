import os
import urllib.request

def download_file(url, filename):
    print(f"다운로드 중: {url} -> {filename}")
    try:
        urllib.request.urlretrieve(url, filename)
        print("다운로드 완료!")
    except Exception as e:
        print(f"다운로드 실패: {e}")

if __name__ == "__main__":
    # YAMNet ONNX 모델 다운로드 URL
    onnx_url = "https://huggingface.co/zeropointnine/yamnet-onnx/resolve/main/yamnet.onnx"
    # YAMNet 클래스 맵 CSV 다운로드 URL
    class_map_url = "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"

    # 현재 디렉토리에 저장
    download_file(onnx_url, "yamnet.onnx")
    download_file(class_map_url, "yamnet_class_map.csv")
