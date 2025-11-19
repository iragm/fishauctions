# Vosk Speech Recognition Model

This directory should contain the Vosk speech recognition model for voice-controlled bid recording.

## Required Model

Download the **vosk-model-small-en-us-0.15** model from:
https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip

## Installation Steps

1. Download the model zip file from the link above
2. Extract the contents
3. Place the extracted `vosk-model-small-en-us-0.15` directory here
4. The final structure should be:
   ```
   auctions/static/models/vosk/vosk-model-small-en-us-0.15/
   ├── am/
   │   └── final.mdl
   ├── conf/
   │   ├── mfcc.conf
   │   └── model.conf
   ├── graph/
   │   ├── HCLr.fst
   │   ├── Gr.fst
   │   └── disambig_tid.int
   ├── ivector/
   │   ├── final.dubm
   │   ├── final.ie
   │   └── final.mat
   └── README
   ```

## How the Model is Loaded

The Vosklet library accesses the model directory via HTTP requests to individual files:
- Model URL: `/static/models/vosk/vosk-model-small-en-us-0.15/`
- The library loads files like:
  - `/static/models/vosk/vosk-model-small-en-us-0.15/conf/model.conf`
  - `/static/models/vosk/vosk-model-small-en-us-0.15/am/final.mdl`
  - etc.

**Important**:
- The model URL must end with a trailing slash (`/`)
- Vosklet will fetch individual files from within the directory
- nginx serves these files with Cross-Origin headers to enable WebAssembly
- Directory listing is NOT required - only file access

## Model Size

The model is approximately 40MB compressed (~80MB extracted) and will be served as static files.

## Verification

After installation, you can verify the model is accessible by checking:
- http://your-domain/static/models/vosk/vosk-model-small-en-us-0.15/README
- http://your-domain/static/models/vosk/vosk-model-small-en-us-0.15/conf/model.conf

Both URLs should return file contents (not 403 or 404 errors).

## Usage

The voice recognition feature in the "Set Lot Winners" page will automatically use this model when available. If the model files are not present, the voice recognition will fail to initialize with an error message about the model not being found.
