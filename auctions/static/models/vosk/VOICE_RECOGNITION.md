# Voice Recognition Feature

## Overview
The "Set Lot Winners" page includes voice recognition powered by Vosklet and Vosk speech recognition models. This allows auctioneers to record lot sales hands-free by speaking commands.

## Supported Voice Commands

### Lot Numbers
- "lot 123" → Sets lot number to 123
- "lot one-two-three" → Sets lot number to 123
- "lot number five" → Sets lot number to 5
- "lot 1 dash 2" → Sets lot number to 12

### Bidder Numbers
- "bidder 5" → Sets winner to bidder 5
- "bidder one-two-three" → Sets winner to bidder 123
- "bidder A" → Sets winner to bidder A
- Works with numbers, letters, and words

### Prices
- "five dollars" → Sets price to $5
- "10 dollar" → Sets price to $10
- "twenty dollars" → Sets price to $20
- Always use "dollar" or "dollars" after the amount

### Special Commands
- **"sold"** → Triggers auto-save when all fields (lot, price, winner) are filled
- **"undo"** → Immediately triggers the undo action for the last sold lot

## Complete Example
Say: **"lot 123 sold to bidder 5 for 10 dollars"**

The system will:
1. Fill in lot number: 123
2. Fill in winner: 5
3. Fill in price: 10
4. Auto-submit the form after "sold" is spoken

## How It Works

### Starting Voice Recognition
1. Click the "Start Voice Control" button
2. Allow microphone access when prompted by your browser
3. The button will turn red and show "Stop Listening"
4. A status message will display what's being recognized

### Stopping Voice Recognition
- Click the "Stop Listening" button (red)
- The button will turn blue again

### Browser Compatibility
- Works in modern browsers that support Web Audio API
- Requires HTTPS in production (works with HTTP on localhost)
- Best results in Chrome, Edge, and Safari

### Privacy
- All speech recognition happens locally in your browser
- No audio is sent to external servers
- The Vosk model runs entirely client-side

## Troubleshooting

### Button Not Visible
- Ensure the Vosk model is properly installed (see README.md)
- Check browser console for errors
- Verify static files are collected: `python manage.py collectstatic`

### Recognition Not Working
- Check microphone permissions in browser settings
- Speak clearly and at a moderate pace
- Ensure there's minimal background noise
- Wait for status to show "Listening..." before speaking

### Commands Not Recognized
- Say "lot", "bidder", "sold", "undo" clearly
- Numbers work best when spoken individually: "one two three" instead of "one hundred twenty-three"
- Use "dollar" or "dollars" for prices
- Pause briefly between different commands

### Accuracy Issues
- Position microphone appropriately
- Speak in a quiet environment
- Consider using a dedicated microphone
- The model works best with American English accents

## Technical Details

### Dependencies
- **Vosklet**: JavaScript library for Vosk integration
- **Vosk Model**: vosk-model-small-en-us-0.15 (40MB)
- **Web Audio API**: Browser audio processing

### Architecture
```
Browser Microphone
    ↓
Web Audio API (echo cancellation, noise suppression)
    ↓
Vosklet Library
    ↓
Vosk Model (local speech recognition)
    ↓
JavaScript Handler (parse commands)
    ↓
Form Fields (auto-fill)
    ↓
AJAX Submit (existing form logic)
```

### Number Parsing
The system converts spoken numbers to digits:
- Words like "one", "two", "three" → "1", "2", "3"
- "twenty" → "20"
- "one-two-three" → "123"
- Already numeric input passes through

### Auto-Submit Logic
1. Spoken commands fill the form fields
2. Fields trigger blur events to validate
3. When "sold" is spoken, system checks if all fields are filled
4. If complete, auto-submits using existing AJAX form handler
5. Form clears and focuses on lot field for next entry

## Performance
- Initial load time: ~2-5 seconds (model loading)
- Recognition latency: Near real-time (< 500ms)
- CPU usage: Moderate (runs in background)
- Model size: ~40MB (cached after first load)

## Future Enhancements
Possible improvements:
- Support for more languages
- Custom wake word detection
- Batch processing of multiple lots
- Voice feedback confirmation
- Training on auction-specific vocabulary
