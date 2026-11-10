#!/usr/bin/env bash

sudo apt update -y
sudo apt install -y python3 python3-venv

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install PyQt6 av pyinstaller


#python SR.py

pyinstaller --clean --onefile --windowed --name SRPlayer --add-data "Data:Data" --icon SR.ico SR.py
