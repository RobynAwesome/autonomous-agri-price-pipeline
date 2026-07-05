import os
import json
import requests
import pandas as pd
import datetime

# Set matplotlib backend to Agg (non-interactive) before importing pyplot
# to ensure it runs without errors in headless environments.
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# List of sub-Saharan African country ISO3 codes to filter by
SSA_ISO3 = {
    'AGO', 'BEN', 'BWA', 'BFA', 'BDI', 'CPV', 'CMR', 'CAF', 'TCD', 'COM', 'COD', 'COG',
    'CIV', 'DJI', 'GNQ', 'ERI', 'SWZ', 'ETH', 'GAB', 'GMB', 'GHA', 'GIN', 'GNB', 'KEN',
    'LSO', 'LBR', 'MDG', 'MWI', 'MLI', 'MRT', 'MUS', 'MOZ', 'NAM', 'NER', 'NGA', 'RWA',
    'STP', 'SEN', 'SYC', 'SLE', 'SOM', 'ZAF', 'SSD', 'SDN', 'TZA', 'TGO', 'UGA', 'ZMB', 'ZWE'
}

def fetch_dataset_resources():
    """Queries the HDX CKAN API to retrieve resources of the Global Food Prices dataset."""
    api_url = "https://data.humdata.org/api/3/action/package_show?id=global-wfp-food-prices"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    print("Fetching WFP Global Food Prices dataset metadata from HDX...")
    response = requests.get(api_url, headers=headers)
    response.raise_for_status()
    
    data = response.json()
    if not data.get("success"):
        raise Exception("HDX API call was not successful.")
        
    return data.get("result", {}).get("resources", [])

def download_and_filter_csv(url, year, temp_dir):
    """
    Downloads the WFP food prices CSV for a specific year, streams it,
    filters in chunks for sub-Saharan Africa and Maize/Rice commodities,
    and returns a pandas DataFrame.
    """
    os.makedirs(temp_dir, exist_ok=True)
    temp_file = os.path.join(temp_dir, f"temp_{year}.csv")
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    print(f"Downloading CSV for {year}...")
    
    # Stream the file to disk to manage memory effectively
    with requests.get(url, headers=headers, stream=True) as r:
        r.raise_for_status()
        with open(temp_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    
    print(f"Finished downloading {year}. Filtering rows in chunks...")
    
    filtered_chunks = []
    chunksize = 100000
    
    # Process the CSV file chunk-by-chunk using pandas
    for chunk_df in pd.read_csv(temp_file, chunksize=chunksize, low_memory=False):
        # 1. Filter by sub-Saharan Africa ISO3 country codes
        chunk_df = chunk_df[chunk_df['countryiso3'].isin(SSA_ISO3)]
        
        # 2. Filter for Maize and Rice commodities
        chunk_df = chunk_df[chunk_df['commodity'].notna()]
        comm_lower = chunk_df['commodity'].str.lower()
        is_target = comm_lower.str.contains('maize') | comm_lower.str.contains('rice')
        chunk_df = chunk_df[is_target]
        
        if not chunk_df.empty:
            filtered_chunks.append(chunk_df)
            
    # Clean up temp file from disk
    if os.path.exists(temp_file):
        os.remove(temp_file)
        
    if filtered_chunks:
        merged_df = pd.concat(filtered_chunks, ignore_index=True)
        print(f"Retrieved {len(merged_df)} matching SSA maize/rice records for {year}.")
        return merged_df
    else:
        print(f"No matching records found for {year}.")
        return pd.DataFrame()

def clean_data(df):
    """
    Cleans the compiled dataset:
    - Drops rows missing crucial values (date, price, usdprice, countryiso3, commodity).
    - Standardizes datatypes (prices and coordinates to numeric float, date to datetime).
    - Filters out invalid price values (<= 0).
    - Standardizes text strings (strips trailing/leading spaces).
    - Classifies specific Maize and Rice broad types.
    - Removes duplicate observations.
    """
    if df.empty:
        print("Empty DataFrame. Skipping cleaning.")
        return df
        
    print(f"Initial raw rows to clean: {len(df)}")
    
    # 1. Copy DataFrame to avoid SettingWithCopyWarning and drop null critical fields
    df = df.copy()
    df = df.dropna(subset=['date', 'price', 'usdprice', 'countryiso3', 'commodity'])
    
    # 2. Fix dates
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date'])
    df = df.sort_values('date')
    
    # 3. Handle numbers and range bounds
    numeric_cols = ['price', 'usdprice', 'latitude', 'longitude']
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
        
    df = df[df['price'] > 0]
    df = df[df['usdprice'] > 0]
    
    # 4. Standardize text strings
    text_cols = ['countryiso3', 'commodity', 'market', 'unit', 'currency', 'pricetype']
    for col in text_cols:
        df[col] = df[col].astype(str).str.strip()
        
    # 5. Unit Normalization to standard "per KG" prices
    import re
    def get_kg_factor(row):
        unit_str = str(row['unit']).upper().strip()
        country = row['countryiso3']
        
        # Somalia special case: all KG units are 50 KG bags in WFP dataset
        if country == 'SOM' and unit_str == 'KG':
            return 50.0
            
        if unit_str == 'KG':
            return 1.0
        if unit_str == '400 G':
            return 0.4
        if 'TIN (20 L)' in unit_str:
            return 16.0
            
        match = re.search(r'([\d.]+)\s*KG', unit_str)
        if match:
            return float(match.group(1))
            
        match_any = re.search(r'([\d.]+)', unit_str)
        if match_any:
            return float(match_any.group(1))
            
        return 1.0

    df['kg_factor'] = df.apply(get_kg_factor, axis=1)
    df['price'] = df['price'] / df['kg_factor']
    df['usdprice'] = df['usdprice'] / df['kg_factor']
    
    # Drop extreme outliers where price per KG exceeds $8.0 USD or is below $0.01 USD (data entry or currency reform errors)
    df = df[(df['usdprice'] <= 8.0) & (df['usdprice'] >= 0.01)]
    
    # 6. Classify primary commodity categories
    def classify_commodity(comm_name):
        c_lower = comm_name.lower()
        if 'maize' in c_lower:
            return 'Maize'
        elif 'rice' in c_lower:
            return 'Rice'
        return 'Other'
        
    df['commodity_type'] = df['commodity'].apply(classify_commodity)
    df = df[df['commodity_type'] != 'Other']
    
    # 7. Deduplicate rows
    df = df.drop_duplicates()
    
    print(f"Cleaned dataset rows: {len(df)}")
    return df

def generate_visual_trend(df, output_img):
    """Generates and saves a trend line chart comparing monthly average USD prices of Maize and Rice."""
    if df.empty:
        print("No data to plot.")
        return
        
    print("Generating price trend visualization...")
    df_plot = df.copy()
    df_plot['year_month'] = df_plot['date'].dt.to_period('M')
    
    # Compute monthly average prices by Country, Commodity type
    monthly_avg = df_plot.groupby(['year_month', 'countryiso3', 'commodity_type'])['usdprice'].mean().reset_index()
    monthly_avg = monthly_avg.sort_values('year_month')
    monthly_avg['year_month_str'] = monthly_avg['year_month'].astype(str)
    
    # To keep the chart clean, find top 5 countries by data volume
    top_countries = df['countryiso3'].value_counts().head(5).index.tolist()
    print(f"Top 5 SSA countries plotted in chart: {top_countries}")
    
    fig, axes = plt.subplots(2, 1, figsize=(12, 10), sharex=True)
    
    # Assign distinct colors to countries
    country_colors = {
        top_countries[0]: '#1f77b4',  # blue
        top_countries[1]: '#ff7f0e',  # orange
        top_countries[2]: '#2ca02c',  # green
        top_countries[3]: '#d62728',  # red
        top_countries[4]: '#9467bd'   # purple
    }
    
    for idx, comm in enumerate(['Maize', 'Rice']):
        ax = axes[idx]
        comm_data = monthly_avg[
            (monthly_avg['commodity_type'] == comm) & 
            (monthly_avg['countryiso3'].isin(top_countries))
        ]
        
        for country in top_countries:
            c_data = comm_data[comm_data['countryiso3'] == country]
            if not c_data.empty:
                ax.plot(
                    c_data['year_month_str'], c_data['usdprice'],
                    marker='o', markersize=4, linewidth=2,
                    label=country, color=country_colors[country]
                )
                
        ax.set_title(f"Average Monthly {comm} Prices (USD/KG) - Top SSA Countries", fontsize=13, fontweight='bold')
        ax.set_ylabel("USD per KG")
        ax.grid(True, linestyle='--', alpha=0.5)
        ax.legend(title="Country ISO3", loc="upper left")
        
    plt.xticks(rotation=45)
    plt.xlabel("Month (Year-Month)")
    plt.tight_layout()
    
    os.makedirs(os.path.dirname(output_img), exist_ok=True)
    plt.savefig(output_img, dpi=300)
    plt.close()
    print(f"Chart saved to {output_img}")

def generate_markdown_report(df, output_md, img_relative_path):
    """Generates the PRICES_TREND.md file including the embedded visualization and price changes."""
    print("Writing markdown report...")
    
    # Aggregation for summary table: get latest month and previous month's price per country
    df_rep = df.copy()
    df_rep['year_month'] = df_rep['date'].dt.to_period('M')
    
    monthly_summary = df_rep.groupby(['year_month', 'countryiso3', 'commodity_type'])['usdprice'].mean().reset_index()
    monthly_summary = monthly_summary.sort_values(['commodity_type', 'countryiso3', 'year_month'])
    
    tables_md = []
    
    for comm in ['Maize', 'Rice']:
        tables_md.append(f"### Recent {comm} Prices by Country\n\n")
        tables_md.append("| Country (ISO3) | Latest Month | Current Price (USD/KG) | Previous Month Price | Monthly Change | Trend |\n")
        tables_md.append("|---|---|---|---|---|---|\n")
        
        comm_summary = monthly_summary[monthly_summary['commodity_type'] == comm]
        
        for country in sorted(comm_summary['countryiso3'].unique()):
            c_data = comm_summary[comm_summary['countryiso3'] == country].tail(2)
            if not c_data.empty:
                latest = c_data.iloc[-1]
                latest_price = latest['usdprice']
                latest_month = str(latest['year_month'])
                
                prev_price = None
                change_str = "N/A"
                trend_symbol = "➖"
                
                if len(c_data) == 2:
                    prev = c_data.iloc[0]
                    prev_price = prev['usdprice']
                    diff = latest_price - prev_price
                    pct = (diff / prev_price) * 100
                    change_str = f"{diff:+.3f} ({pct:+.1f}%)"
                    
                    if pct > 1.0:
                        trend_symbol = "📈"
                    elif pct < -1.0:
                        trend_symbol = "📉"
                        
                prev_str = f"${prev_price:.3f}" if prev_price is not None else "N/A"
                tables_md.append(f"| {country} | {latest_month} | ${latest_price:.3f} | {prev_str} | {change_str} | {trend_symbol} |\n")
        tables_md.append("\n")
        
    tables_content = "".join(tables_md)
    
    # Markdown Template
    report = f"""# Sub-Saharan Africa Agricultural Price Trend Report

*Generated automatically on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*

This report monitors and visualizes retail and wholesale prices for **Maize** and **Rice** in sub-Saharan Africa. The data source is the WFP Global Food Prices Database.

---

## Price Trend Visualization

![Price Trends]({img_relative_path})

---

## Recent Monthly Price Analysis

{tables_content}

> [!NOTE]
> * Prices reflect WFP local retail/wholesale price data converted to USD equivalent per KG.
> * Trend indicator threshold is set at a 1.0% change relative to the previous observation month.
> * `📈` indicates price increase, `📉` indicates price decrease, and `➖` represents price stability (within +/- 1.0%).

## Data Insights and Summary

* **Maize**: Displays high geographic price variance. Prices are heavily influenced by local harvests, transport costs, and cross-border trade policies.
* **Rice**: Rice exhibits more uniform pricing across countries, as it is heavily imported from global markets (e.g. Southeast Asia) and tied to international pricing benchmarks.

*Cleaned source data file is available at: [cleaned_prices.csv](data/cleaned_prices.csv)*
"""
    with open(output_md, "w") as f:
        f.write(report)
        
    print(f"Markdown report generated successfully at {output_md}")

def run_pipeline():
    """Main function executing all steps of the pipeline."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "data")
    temp_dir = os.path.join(data_dir, "temp")
    
    # Fetch resources from HDX CKAN API
    resources = fetch_dataset_resources()
    
    # Collect matching resources for years 2024, 2025, 2026
    target_years = ['2026', '2025', '2024']
    year_urls = {}
    for r in resources:
        name = r.get("name", "")
        url = r.get("url", "")
        for yr in target_years:
            if yr in name and r.get("format", "").upper() == "CSV":
                year_urls[yr] = url
                break
                
    # Download, filter and load datasets
    all_data = []
    for yr in target_years:
        if yr in year_urls:
            url = year_urls[yr]
            try:
                yr_df = download_and_filter_csv(url, yr, temp_dir)
                if not yr_df.empty:
                    all_data.append(yr_df)
            except Exception as e:
                print(f"Error processing data for {yr}: {e}")
                
    if not all_data:
        raise Exception("Failed to scrape or process any year's data.")
        
    # Combine data from all years
    combined_df = pd.concat(all_data, ignore_index=True)
    print(f"Total compiled raw records: {len(combined_df)}")
    
    # Clean data
    cleaned_df = clean_data(combined_df)
    
    # Save cleaned data
    os.makedirs(data_dir, exist_ok=True)
    csv_output_path = os.path.join(data_dir, "cleaned_prices.csv")
    cleaned_df.to_csv(csv_output_path, index=False)
    print(f"Cleaned dataset saved successfully to {csv_output_path}")
    
    # Generate visualization
    plot_output_path = os.path.join(data_dir, "price_trends.png")
    generate_visual_trend(cleaned_df, plot_output_path)
    
    # Generate Markdown report
    report_output_path = os.path.join(base_dir, "PRICES_TREND.md")
    generate_markdown_report(cleaned_df, report_output_path, "data/price_trends.png")
    
    # Clean up temp folder
    if os.path.exists(temp_dir):
        try:
            os.rmdir(temp_dir)
        except Exception:
            pass
            
    print("Agricultural price pipeline complete!")

if __name__ == "__main__":
    run_pipeline()
