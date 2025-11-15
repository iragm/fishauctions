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
   ├── conf/
   ├── graph/
   ├── ivector/
   └── README
   ```

## Model Size

The model is approximately 40MB compressed and will be served as static files.

## Usage

The voice recognition feature in the "Set Lot Winners" page will automatically use this model when available. If the model is not present, the voice recognition button will not be displayed.
