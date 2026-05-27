import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

def main():
    print("Initializing Kokoro ONNX model...")
    k = Kokoro('kokoro-v1.0.onnx', 'voices-v1.0.bin')
    
    text = "Hello sir, I am F.R.I.D.A.Y., your personal AI assistant. Let me know what you need today."
    
    # Base voices
    v_emma = k.get_voice_style('bf_emma')       # Base British (confident, clean)
    v_isabella = k.get_voice_style('bf_isabella') # Soft British (warm)
    v_lily = k.get_voice_style('bf_lily')       # Clear British
    v_sarah = k.get_voice_style('af_sarah')     # Expressive US (high emotion)
    v_nicole = k.get_voice_style('af_nicole')   # Expressive US (warm)
    v_sara_it = k.get_voice_style('if_sara')    # Italian female (melodic cadence)
    v_dora_es = k.get_voice_style('ef_dora')    # Spanish female (soft consonants)
    v_siwis_fr = k.get_voice_style('ff_siwis')  # French female (musical intonation)

    # Recipe 1: Friday Irish Rose (Melodic & Warm)
    # Blends elegant British with expressive US and a touch of Italian cadence for a rolling, lyrical lilt
    blend1 = 0.50 * v_emma + 0.35 * v_nicole + 0.15 * v_sara_it
    
    # Recipe 2: Friday Celtic Whisper (Soft & Lyrical)
    # Warm British foundation mixed with expressive US and soft French breathiness/consanants
    blend2 = 0.55 * v_isabella + 0.30 * v_sarah + 0.15 * v_siwis_fr

    # Recipe 3: Friday Gaelic Aurora (Crisp & Confident)
    # Lyrical British base combined with US expressiveness and Spanish soft vowel-to-consonant transitions
    blend3 = 0.50 * v_lily + 0.30 * v_nicole + 0.20 * v_dora_es

    print("Generating Recipe 1: Friday Irish Rose...")
    samples1, sr1 = k.create(text, voice=blend1, speed=1.05, lang='en-gb')
    sf.write('friday_irish_rose.wav', samples1, sr1)

    print("Generating Recipe 2: Friday Celtic Whisper...")
    samples2, sr2 = k.create(text, voice=blend2, speed=1.05, lang='en-gb')
    sf.write('friday_celtic_whisper.wav', samples2, sr2)

    print("Generating Recipe 3: Friday Gaelic Aurora...")
    samples3, sr3 = k.create(text, voice=blend3, speed=1.05, lang='en-gb')
    sf.write('friday_gaelic_aurora.wav', samples3, sr3)

    print("Done! Voice samples generated successfully.")

if __name__ == '__main__':
    main()
