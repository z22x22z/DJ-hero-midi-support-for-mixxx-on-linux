# DJ-hero-midi-support-for-mixxx-on-linux
Use your PlayStation DJ hero ps3/ps2 controller in Mixxx an open-source software for DJing *on linux
this is base on Rosalie Wanders's patch https://ratatoskr.run/linux-input/2026/02/3432164/t so you may need it but the these scripts can translate the controller behaviors to Mixxx 

------------------------------------------------------------------ Installation & Running -------------------------------------------------------------

1-   Create a document in   "/etc/udev/rules.d/" than name it "69-dj-hero.rules" open it and past this --> -- KERNEL=="hidraw*", ATTRS{idVendor}=="12ba", ATTRS{idProduct}=="0140", MODE="0666" -- or this
--> -- SUBSYSTEM=="usb", ATTRS{idVendor}=="12ba", ATTRS{idProduct}=="0140", MODE="0666" -- the new 69-dj-hero.rules will allow your system to see the controller. 

2-   You need python3 on your system however it can come pre-installed in most distribution but you will need to install (python-mido) and (python-rtmidi) so depending on your distro you can use one of these   

---> on Ubuntu / Debian / Mint :sudo apt update && sudo apt install python3 python3-mido python3-rtmidi

---> on Fedora 	               :sudo dnf install python3 python3-mido python3-rtmidi

---> on Arch Linux / Manjaro	 :sudo pacman -Syu python python-mido python-rtmidi

---> on openSUSE	             :sudo zypper install python3 python3-mido ( rtmidi may require pip inside openSUSE) .

---> or just "pip install evdev mido python-rtmidi --break-system-packages" 

3-   Copy both DJ-Hero-Bridge.midi.xml and DJ-Hero-Bridge.js to /home/(your-user-name)/.mixxx/controllers .

4-   Past this in the terminal "python3 djhero_mixxx_bridge.py" .
  .and that kinda it for the bridge Installation
  
 ---------------------------------------------why all these files-----------------------------------------------------------------------

1- DJ-Hero-Bridge.midi.xml tell the app what to do with the DJ-hero controller.

2- DJ-Hero-Bridge.js allow for 2 decks usage and add some features.

3- djhero_ui.html use your browser for the bridge UI as a settings page: http://127.0.0.1:8765/.

4- djhero_mixxx_bridge.py allow all these files to work and give you some configuration on the controller.

-------------------------------------------------- saving settings --------------------------------------------------------

if you change something in the settings UI and press
<img width="78" height="36" alt="Screenshot 2026-10-26 at 03-34-23 DJ Hero Bridge" src="https://github.com/user-attachments/assets/0785c9ff-10c7-4075-919a-0851cd834ae7" />     your new settings will not be apply, you must select      <img width="174" height="36" alt="Screenshot 2026-10-26 at 03-38-07 DJ Hero Bridge" src="https://github.com/user-attachments/assets/58045459-a295-44d6-b0fa-bb80f73ddcdb" />
 than relaunch mixxx for your new settings to be applied.
 
 -------------------------------------------------------------------------------------------------------------------------------------------
 more info about what each button does in end of the settings page: http://127.0.0.1:8765/.
