# login.py
# Zerodha Auto Login + Auto .env Token Update (PRO)

import os
import time
import urllib.parse

import pyotp
from dotenv import load_dotenv
from kiteconnect import KiteConnect

from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


# ================= LOAD ENV =================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_FILE = os.path.join(BASE_DIR, ".env")

load_dotenv(ENV_FILE, override=True)


# ================= READ VARS =================

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
USER_ID = os.getenv("KITE_USER_ID")
PASSWORD = os.getenv("KITE_PASSWORD")
TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")


# ================= VALIDATION =================

for name, val in {
    "KITE_API_KEY": API_KEY,
    "KITE_API_SECRET": API_SECRET,
    "KITE_USER_ID": USER_ID,
    "KITE_PASSWORD": PASSWORD,
    "KITE_TOTP_SECRET": TOTP_SECRET,
}.items():

    if not val:
        raise RuntimeError(f"{name} missing in .env")


# ================= OTP =================

def generate_otp():

    totp = pyotp.TOTP(TOTP_SECRET)

    otp = totp.now()

    print("Generated OTP:", otp)

    return otp


# ================= EDGE DRIVER =================

def edge_driver():

    options = Options()

    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")

    return webdriver.Edge(options=options)


# ================= TOKEN WAIT =================

def wait_for_request_token(driver, timeout=180):

    start = time.time()

    while time.time() - start < timeout:

        url = driver.current_url

        if "request_token=" in url:

            parsed = urllib.parse.urlparse(url)

            token = urllib.parse.parse_qs(parsed.query).get("request_token")

            if token:

                return token[0]

        time.sleep(0.5)


    raise TimeoutException("request_token not found")


# ================= UPDATE .ENV =================

def update_env_token(token):

    lines = []

    found = False


    # Read existing
    if os.path.exists(ENV_FILE):

        with open(ENV_FILE, "r") as f:

            for line in f:

                if line.startswith("KITE_ACCESS_TOKEN="):

                    lines.append(f"KITE_ACCESS_TOKEN={token}\n")

                    found = True

                else:
                    lines.append(line)


    # Add if missing
    if not found:

        lines.append(f"\nKITE_ACCESS_TOKEN={token}\n")


    # Write back
    with open(ENV_FILE, "w") as f:

        f.writelines(lines)


    print("✅ .env updated with new access token")


# ================= LOGIN =================

def login_and_get_token():

    login_url = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}"

    print("Opening:", login_url)


    driver = edge_driver()

    driver.get(login_url)

    wait = WebDriverWait(driver, 40)


    try:

        # USER ID
        user_box = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='text']"))
        )

        user_box.send_keys(USER_ID)
        user_box.send_keys(Keys.RETURN)


        # PASSWORD
        pass_box = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='password']"))
        )

        pass_box.send_keys(PASSWORD)
        pass_box.send_keys(Keys.RETURN)


        # OTP
        try:

            otp_box = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='password']"))
            )

            otp_box.send_keys(generate_otp())
            otp_box.send_keys(Keys.RETURN)

            print("OTP submitted")

        except TimeoutException:

            print("OTP skipped")


        print("Waiting for redirect...")

        token = wait_for_request_token(driver)

        print("Request token:", token)

        return token


    finally:

        driver.quit()


# ================= MAIN =================

if __name__ == "__main__":

    request_token = login_and_get_token()


    kite = KiteConnect(api_key=API_KEY)

    session = kite.generate_session(
        request_token,
        api_secret=API_SECRET
    )


    access_token = session["access_token"]

    print("Access Token:", access_token)


    # Save backup
    with open("access_token.txt", "w") as f:

        f.write(access_token)


    # Update .env
    update_env_token(access_token)


    print("✅ Login + Token Sync Complete")
