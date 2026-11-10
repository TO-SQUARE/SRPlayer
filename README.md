# SRPlayer v1

مشغّل صوتيات مكتبي بواجهة بسيطة وأنيقة. يتيح لك تشغيل ملفاتك الصوتية وتنظيمها في قوائم تشغيل، والاستماع إلى محطات الراديو عبر الإنترنت أو البث الصوتي، وتسجيل الصوت الجاري تشغيله.

## المزايا

- تشغيل ملفات **MP3 وWAV وOGG وFLAC وM4A وAAC**.
- فتح ملفات متعددة أو إضافة مجلد كامل إلى قائمة التشغيل.
- إنشاء قوائم تشغيل وإعادة تسميتها وحذفها، وإضافة شعار مخصص لكل قائمة.
- إضافة محطات راديو عبر رابط HTTP أو HTTPS وتعديل اسم المحطة ورابطها.
- التحكم في مستوى الصوت، مع معادل صوتي ومؤثرات صوتية قابلة للتخصيص.
- تسجيل الصوت الجاري تشغيله بصيغ **WAV وMP3 وAAC (M4A)**.
- يمكن أختيار مسار لحفظ ملفات الصوت التي تقوم بتسجلها.
- تشغيل عشوائي وتكرار المقاطع، والبحث داخل قائمة التشغيل.
- استخدام مشغّل مصغّر وأيقونة منطقة الإعلام عند توفرها.

## التشغيل

يتطلب Python 3 ونظام التشغيل Windows أو Linux. من مجلد المشروع، ثبّت المتطلبات ثم شغّل التطبيق:

```powershell
py -3 -m pip install -r requirements.txt
py -3 SR.py
```

## إنشاء نسخة تنفيذية

Windows: 
شغّل `build.bat` لإنشاء `dist\SRPlayer.exe`. 
يثبّت الملف متطلبات المشروع وPyInstaller، ثم يبني نسخة تنفيذية واحدة.

أو نفّذ الأمر التالي يدويًا من مجلد المشروع:

```
py -3 -m pip install -r requirements.txt pyinstaller
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name SRPlayer --add-data "Data;Data" --icon SR.ico SR.py
```

Linux: 
شغل الامر التالي في الترمنال داخل مسار ملف الـ python

شغل ملف Shell script في الترمنال بمسار مجلد المصدر وهو ملف build_linux_debian.sh
```
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name SRPlayer --add-data "Data;Data" --icon SR.ico SR.py
#!/usr/bin/env bash
sudo apt update -y
sudo apt install -y python3 python3-venv
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install PyQt6 av pyinstaller

pyinstaller --clean --onefile --windowed --name SRPlayer --add-data "Data:Data" --icon SR.ico SR.py

```
بعد تشغيل الملف سوف يتم أنشاء مجلد dist وبه ملف البرنامج ويمكن تداوله كنسخة محمولة

## الملفات التي ينشئها التطبيق

يحفظ التطبيق بياناته بجوار ملفات البرنامج:

- `SAVE\playlists.json`: قوائم التشغيل ومصادرها وأسماؤها وأغلفتها.
- `SAVE\audio_settings.json`: إعدادات الصوت والتسجيل.
- `SAVE\window_settings.json`: إعدادات النافذة والمشغّل المصغّر.
- `Recordings\`: التسجيلات التي تنشئها داخل التطبيق.
تُخزّن هذه الملفات محليًا؛ وقد تتضمن قوائم التشغيل مسارات ملفات الصوت على جهازك أو روابط محطات الراديو التي أضفتها.

## محتويات المشروع

- `SR.py`: التطبيق والواجهة ومنطق تشغيل الصوت.
- `Data\`: أيقونات التطبيق وشعاره والخطوط المضمّنة.
- `requirements.txt`: متطلبات تشغيل التطبيق (مكتبات python).
