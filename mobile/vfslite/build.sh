#!/usr/bin/env bash
# Build and install the watch app. No Gradle, no Android Studio: aapt2 -> javac -> d8 -> apksigner.
#
#   ./build.sh            # build app.apk
#   ./build.sh install    # build, then adb install -r onto the connected phone
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_SDK:-$HOME/android/sdk}"
JDK="${JAVA_HOME:-$HOME/android/jdk-17.0.20.1+1}"
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
export PATH="$JDK/bin:$BT:$SDK/platform-tools:$PATH"

for f in "$BT/aapt2" "$PLATFORM" "$JDK/bin/javac"; do
  [ -e "$f" ] || { echo "missing: $f  (set ANDROID_SDK / JAVA_HOME)"; exit 1; }
done

rm -rf build && mkdir -p build/{res,gen,classes,dex}

echo "1/5 resources"
"$BT/aapt2" compile --dir res -o build/res.zip
"$BT/aapt2" link -o build/base.apk -I "$PLATFORM" \
  --manifest AndroidManifest.xml --java build/gen --auto-add-overlay build/res.zip

echo "2/5 java"
"$JDK/bin/javac" -source 17 -target 17 -nowarn -classpath "$PLATFORM" -d build/classes \
  $(find src build/gen -name '*.java')

echo "3/5 dex"
"$BT/d8" --min-api 26 --output build/dex $(find build/classes -name '*.class') --lib "$PLATFORM"

echo "4/5 package"
cp build/base.apk build/app.unsigned.apk
(cd build/dex && zip -q -r ../app.unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/app.unsigned.apk build/app.aligned.apk

echo "5/5 sign"
[ -f debug.keystore ] || "$JDK/bin/keytool" -genkeypair -keystore debug.keystore -storepass android \
  -keypass android -alias vfslite -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=vfslite, OU=4indegree, O=AAI, L=Kochi, C=IN" >/dev/null 2>&1
"$BT/apksigner" sign --ks debug.keystore --ks-pass pass:android --key-pass pass:android \
  --out app.apk build/app.aligned.apk
"$BT/apksigner" verify app.apk && echo "built: $(pwd)/app.apk ($(du -h app.apk | cut -f1))"

if [ "${1:-}" = "install" ]; then
  adb install -r app.apk
  adb shell am start -n com.fourindegree.vfslite/.MainActivity
fi
