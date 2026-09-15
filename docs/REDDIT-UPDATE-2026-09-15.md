# I'm turning my Pocket 4P into a programmable camera rig — OsmoDesk update (WIP)

I wanted more from my Pocket 4P than a remote record button: set up a shot,
shape the movement, rehearse it, then run it again without starting from scratch.
That's why I started building OsmoDesk.

It's still WIP, but there's a lot to play with now:

- **Plan the shot:** save framing points, adjust easing and holds, and preview
  the path before the camera moves.
- **Make it repeatable:** programmed moves, motion timelapse, take notes and
  motion-data exports. The record-and-move workflow waits for recording confirmation.
- **See what you're doing:** cinema monitoring with guides, zebras, peaking,
  waveform, histogram, vectorscope and viewing LUTs.
- **Make it yours:** rearrange the workspace and control it from a desktop or phone.

The new optional extra is **AI Shot Copilot**. Give it a brief like “a slow
reveal with a longer hold at the end” and it offers two timing treatments for
framing points you've already set. Preview them, apply one, or keep your original.
It uses your own OpenAI API key; the rest of OsmoDesk works without AI.
You review the planning data before it's sent to OpenAI; no footage is uploaded.
AI cannot connect, move or record the camera.

[Repo and setup instructions](https://github.com/ElectronicPaper/OsmoDesk)

You need a computer or supported rig host running the Python server; the phone
is a control screen. Tested on my **Pocket 4P only**, not Pocket 3 or 4.

The setup images are AI-generated, with screens based on the real interface.
The country scenes and their demo pictures are generated too, not real test
footage. Other demo captures use [credited sample photos](https://github.com/ElectronicPaper/OsmoDesk/blob/main/docs/images/demo-samples/README.md).
Real setup photos will follow.

I still wish DJI offered an official Pocket Bluetooth SDK—there's a lot the
community could build for these cameras.

What would you try first: product shots, architectural reveals, or motion timelapse?
