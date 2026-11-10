# Hear the Gap

A single-page web app for showing someone who is hard of hearing how loud their
TV is to everyone else. No build step, no server, no dependencies: open
`index.html`.

1. **Find the gap** between their TV volume and what the rest of the house
   would choose, in dB. Three ways: TV volume numbers (rough), a comfort match
   where two people set the same sound to taste, or two phone-microphone
   readings (relative level, so no calibration is needed).
2. **Play it back**: they set a comfortable level, then the page plays the same
   sound louder by the gap (capped at 30 dB, 5 seconds).
3. **Quick hearing check**: five rising tones (500 Hz to 8 kHz). A person with
   good hearing goes first as the reference, so the result is a relative loss
   per pitch and an average graded on the WHO bands.
4. **Result**: the gap as loudness and energy ratios, a dB ladder of everyday
   sounds, an audiogram-style chart, and next steps.

Notes:

- The microphone method needs HTTPS or `localhost` (browser rule). Serve it with
  `python3 -m http.server` from this folder, or host it on any static host.
- It is a demonstration and a rough screen, not a medical test.
