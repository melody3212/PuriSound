import onnxruntime as ort

try:
    session = ort.InferenceSession("yamnet.onnx")
    print("--- 입력 노드 정보 ---")
    for idx, node in enumerate(session.get_inputs()):
        print(f"입력 {idx}: 이름={node.name}, 형태={node.shape}, 타입={node.type}")
    
    print("\n--- 출력 노드 정보 ---")
    for idx, node in enumerate(session.get_outputs()):
        print(f"출력 {idx}: 이름={node.name}, 형태={node.shape}, 타입={node.type}")
except Exception as e:
    print(f"에러 발생: {e}")
