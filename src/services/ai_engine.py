import torch
import numpy as np
import laion_clap

def get_clap_model():
    model = laion_clap.CLAP_Module(enable_fusion=False, amodel= 'HTSAT-base')
    model.load_ckpt()
    device="cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.eval()
    print(f"CLAP Model Loaded on {device.upper()}")
    return model, device

def get_vector_size(model, device = "cpu"):
    sr = 48000
    dummy_audio = torch.randn(1, sr).to(device)
    with torch.no_grad():
        audio_embed = model.get_audio_embedding_from_data(x=dummy_audio, use_tensor = True)
        actual_size = audio_embed.shape[-1]
        return actual_size
if __name__ == "__main__":
    try:
        model_instance, device_used = get_clap_model()
        Vector_Size = get_vector_size(model_instance, device_used)
        print(f"Vector size: {Vector_Size}" )
    except Exception as e:
        print("Initialization failed due to : " + str(e))

