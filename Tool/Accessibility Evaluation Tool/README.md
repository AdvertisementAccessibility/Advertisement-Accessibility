# AdMole
This contains the source code and instructions of the tool AdMole that can detect accessibility issues in Mobile Ads.

## Setup
The tool was tested on both Mac OS and Windows.

- (For MacOS X) Install coreutils "brew install coreutils"
- Use Java8, if there are multiple Java versions use [jenv](https://www.jenv.be/)
- Set `ANDROID_HOME` environment varilable 
- Add platform tools to `PATH` (if it's not already added). `export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:${PATH}"`
- (Optional) create a virtual environment in `.env` (`python3 -m venv .env`)
- Run `source env`
- Install python packages `pip install -r requirements.txt`
- Install TalkBack
- Build AdMole Service APK by running `./build_admole_lib.sh`, then install it (`adb install -r -g Setup/Admole.apk`) or install from Android Studio
	- To check if the installation is correct, execute `./scripts/enable-talkback.sh` (by clicking on a GUI element it should be highlighted).
	- Also, execute `./scripts/send-command.sh log` and check Android logs to see if it prints the AccessibilityNodeInfos of GUI element on the screen (`adb logcat | grep "LATTE_SERVICE"`)

## Running AdMole
To analyze the accessibility quality of a mobile ad screen, using the following command. The analyzed results can be found under Analyze_Snapshot/Test/Snapshot 1/ ad_a11y_issues.
```
python main.py --app-name "Test" --output-path "Analyze_Snapshot" --snapshot "Snapshot 1" --debug --device "14061JEC203474" --snapshot-task "analyze_adscreen" --windows --emulator --har-path "Requests.har"
```

**--app-name**: is the name of the tested app.

**--output-path**: is the folder name under py_src to store the analysis result

**--snapshot**: the name of the UI Snapshot, it can be any name.

**--device**: serial number of your Android Device

**--windows**: If you're using the Windows OS

