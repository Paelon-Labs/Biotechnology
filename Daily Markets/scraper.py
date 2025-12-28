import os
import time
import pandas as pd
import glob
from datetime import datetime
from playwright.sync_api import sync_playwright

def archive_old_files(history_dir="../History"):
    """Finds existing Biotechnology CSVs and moves them to History as .gz"""
    if not os.path.exists(history_dir):
        os.makedirs(history_dir)
    
    # Search for any existing Biotechnology CSV files
    old_files = glob.glob("Biotechnology *.csv")
    
    for file_path in old_files:
        try:
            # Create the history filename (e.g., Biotech_12-26-2025.csv.gz)
            # We strip 'Biotechnology ' and '.csv' to keep the date
            date_part = file_path.replace("Biotechnology ", "").replace(".csv", "")
            history_path = os.path.join(history_dir, f"Biotech_{date_part}.csv.gz")
            
            # Read and compress into History
            df_temp = pd.read_csv(file_path)
            df_temp.to_csv(history_path, index=False, compression='gzip')
            
            # Remove the original file from the main folder
            os.remove(file_path)
            print(f"📦 Archived and removed old file: {file_path}")
        except Exception as e:
            print(f"⚠️ Could not archive {file_path}: {e}")

def process_biotech_csv(input_file):
    try:
        # 1. ARCHIVE FIRST: Move yesterday's file to History
        archive_old_files()

        print(f"🔄 Processing new download: {input_file}...")
        df = pd.read_csv(input_file)
        df.columns = [c.strip() for c in df.columns]
        
        # 2. Filter Logic
        target_industries = [
            "Biotechnology: Pharmaceutical Preparations",
            "Biotechnology: Biological Products (No Diagnostic Substances)",
            "Biotechnology: In Vitro & In Vivo Diagnostic Substances"
        ]
        
        industry_col = next((c for c in df.columns if 'industry' in c.lower()), None)
        country_col = next((c for c in df.columns if 'country' in c.lower()), None)

        if not industry_col: return None

        df_filtered = df[df[industry_col].isin(target_industries)].copy()
        #if country_col:
         #   df_filtered = df_filtered[df_filtered[country_col] == 'United States'].copy()

        # 3. Market Cap Conversion
        market_cap_col = next((c for c in df.columns if 'market_cap' in c.lower() or 'market cap' in c.lower()), None)
        if market_cap_col:
            df_filtered[market_cap_col] = df_filtered[market_cap_col].apply(
                lambda x: int(float(str(x).replace('$','').replace(',',''))/1000000) if pd.notnull(x) and str(x).strip() != '' else 0
            )
            df_filtered = df_filtered.sort_values(by=market_cap_col, ascending=False)

        # 4. Save TODAY'S file in the main folder
        current_date = datetime.now().strftime("%m-%d-%Y")
        standard_filename = f"Biotechnology {current_date}.csv"
        df_filtered.to_csv(standard_filename, index=False)
        print(f"✅ Saved today's file: {standard_filename}")

        return standard_filename

    except Exception as e:
        print(f"❌ Processing Error: {e}")
        return None

def run_scraper():
    with sync_playwright() as p:
        print("🚀 Launching Firefox...")
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print("📡 Navigating to Nasdaq...")
        page.goto("https://www.nasdaq.com/market-activity/stocks/screener", timeout=60000)

        try:
            csv_selector = "button.jupiter22-c-table__download-csv"
            page.wait_for_selector(csv_selector, timeout=20000)
            
            with page.expect_download(timeout=60000) as download_info:
                page.locator(csv_selector).click(force=True)
            
            download = download_info.value
            raw_file = "temp_raw_data.csv"
            download.save_as(raw_file)
            
            # This triggers both the daily save and the history archive
            process_biotech_csv(raw_file)
            
            if os.path.exists(raw_file):
                os.remove(raw_file)

        except Exception as e:
            print(f"❌ Scraper Error: {e}")
        
        browser.close()

if __name__ == "__main__":
    run_scraper()