# OsmoDesk update: repeatable Pocket moves, motion timelapse and a proper shot workspace (WIP)

I started OsmoDesk because I wanted to plan and repeat shots with my Pocket 4P,
not just press record from another screen. It's still a work in progress, but
it's grown into quite a bit more than a browser remote.

Some recent additions, alongside the existing controls:

- A shot workspace for saving framing points, setting easing and holds, and
  previewing the path before moving the camera.
- Motion timelapse, and recording workflows that wait for the camera to confirm
  recording before the move starts.
- A/B autofocus targets. AF-S gave me a visible near/far/near change on my 4P.
  Important caveat: this also changes spot metering; it isn't manual rack focus.
- Rearrangeable panels, phone controls, live view, scopes, zebras, peaking and LUT
  monitoring, plus take notes and motion-data exports.
- Optional AI timing suggestions for points you've already set. Bring your own
  API key, preview the suggestions and choose what to apply. AI can't move or
  record with the camera.

Repo: https://github.com/ElectronicPaper/OsmoDesk

You still need a computer or supported rig host running the Python server;
the phone is a control screen. I've tested on my Pocket 4P, not the Pocket 3 or 4.

The setup image is AI-generated, based closely on the actual UI shown in the
screenshots. The demo uses [credited Pocket 3 sample photos](https://github.com/ElectronicPaper/OsmoDesk/blob/main/docs/images/demo-samples/README.md),
not live footage. Real setup photos will follow.

I still wish DJI would offer an official Pocket Bluetooth SDK. There are so many
useful little tools people could build around these cameras.

If you try it, I'd love to hear what would make it useful for your own shots.
