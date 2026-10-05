#!/bin/bash
# SelfMail Android APK 构建脚本（Linux + JDK 21 环境运行，工具自动下载）
# 用法: bash scripts/apk/build-apk.sh
set -e
cd "$(dirname "$0")/../../.."   # 项目根
TOOL=$(mktemp -d); A=tmp/apkbuild
mkdir -p "$A/out" "$A/res/values" "$A/src/com/selfmail/app" dist/android

echo "[1/6] 下载构建工具（aapt2 / r8 / apksig / android.jar）"
AAPT2_V=$(curl -sL https://dl.google.com/android/maven2/com/android/tools/build/aapt2/maven-metadata.xml \
          | grep -o "<version>8\.7\.[0-9]*-[0-9]*" | tail -1 | cut -c10-)
curl -sL -o "$TOOL/aapt2.jar" "https://dl.google.com/android/maven2/com/android/tools/build/aapt2/$AAPT2_V/aapt2-$AAPT2_V-linux.jar"
unzip -oq "$TOOL/aapt2.jar" aapt2 -d "$TOOL" && chmod +x "$TOOL/aapt2"
R8_V=$(curl -sL https://dl.google.com/android/maven2/com/android/tools/r8/maven-metadata.xml \
       | grep -o "<version>[^<]*" | tail -1 | cut -c10-)
curl -sL -o "$TOOL/r8.jar" "https://dl.google.com/android/maven2/com/android/tools/r8/$R8_V/r8-$R8_V.jar"
curl -sL -o "$TOOL/apksig.jar" "https://dl.google.com/android/maven2/com/android/tools/build/apksig/8.7.3/apksig-8.7.3.jar"
curl -sL -o "$TOOL/android.jar" "https://repo1.maven.org/maven2/com/google/android/android/4.1.1.4/android-4.1.1.4.jar"

echo "[2/6] 生成图标与源码"
python3 scripts/apk/make_icon.py
# 源码与资源由本目录提供（AndroidManifest.xml / MainActivity.java / strings.xml）

echo "[3/6] aapt2 编译资源"
"$TOOL/aapt2" compile --dir scripts/apk/res -o $A/out/res.zip
"$TOOL/aapt2" link -o $A/out/base.apk -I "$TOOL/android.jar" \
    --manifest scripts/apk/AndroidManifest.xml $A/out/res.zip \
    --min-sdk-version 24 --target-sdk-version 34 --auto-add-overlay

echo "[4/6] 编译 Java 并转 DEX"
java -cp scripts/apk/ecj.jar org.eclipse.jdt.internal.compiler.batch.Main -1.8 \
     -classpath "$TOOL/android.jar" -d $A/out/classes scripts/apk/MainActivity.java
(cd $A/out/classes && zip -qr ../classes.jar .)
mkdir -p $A/out/dex
java -cp "$TOOL/r8.jar" com.android.tools.r8.D8 --release --lib "$TOOL/android.jar" \
     --output $A/out/dex $A/out/classes.jar
zip -j $A/out/base.apk $A/out/dex/classes.dex

echo "[5/6] 生成签名密钥（如无）"
KS=scripts/apk/selfmail.keystore
[ -f "$KS" ] || keytool -genkeypair -keystore "$KS" -storepass selfmail2026 \
    -keypass selfmail2026 -alias selfmail -keyalg RSA -keysize 2048 -validity 10950 \
    -dname "CN=SelfMail, OU=SelfMail, O=SelfMail, C=CN"

echo "[6/6] 签名（v2+v3）"
java -cp "$TOOL/apksig.jar" scripts/apk/ApkSignerMain $A/out/base.apk \
     dist/android/SelfMail-1.0.0.apk "$KS" selfmail2026

echo "完成: dist/android/SelfMail-1.0.0.apk"
