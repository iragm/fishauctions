# Demo Data Fixture

This directory contains the demo data fixture that is automatically loaded when starting the application in development mode (DEBUG=True) with an empty database.

## What Gets Loaded

The `demo_data.json` fixture includes:

- **4 Demo Users**: admin, sellers, and a bidder with appropriate permissions
- **4 Categories**: Cichlids, Livebearers, Plants, Equipment
- **3 Auctions**:
  - In-person auction (Spring 2024)
  - Active online auction (ends Dec 25, 2025) - currently running
  - Ended online auction (Fall 2024) - already completed
- **7 Pickup Locations**: Including multiple locations and mail shipping options
- **11 AuctionTOS entries**: Representing user participation in auctions (admin/non-admin)
- **13 Lots**: In various states (active, sold, unsold, with/without buy now)
- **9 Bids**: Sample bidding activity

## How It Works

The `load_demo_data` management command is automatically called during container startup via `entrypoint.sh`. It:

1. Checks if DEBUG=True
2. Checks if any auctions exist in the database
3. If both conditions are met, loads the `demo_data.json` fixture

This is idempotent - running it multiple times is safe. It will skip loading if data already exists.

## Updating the Demo Data

### Important Note About Dates

The fixture contains hardcoded dates throughout. The "Demo Online Auction - Active Now!" (pk=9002) has its end date set to December 25, 2025. When this date passes, you'll need to update multiple date fields to keep the auction active:

- `auctions.auction` pk=9002: `date_start`, `date_end`, `lot_submission_start_date`, `lot_submission_end_date`, `date_online_bidding_starts`, `date_online_bidding_ends`
- `auctions.pickuplocation` pks 9002-9004: `pickup_time`, `second_pickup_time`
- `auctions.lot` pks 90004-90008: `date_end`

**Quick fix**: Search for "2025-12-" in the fixture and update all occurrences to a future date.

### Method 1: Manual Editing

Edit `demo_data.json` directly. The file is standard Django fixture JSON format.

**Important**:
- Use PKs in the 9000+ range to avoid conflicts with real data
- Maintain referential integrity (e.g., lots must reference valid auctions, users, etc.)
- Use ISO 8601 date format: `YYYY-MM-DDTHH:MM:SSZ`

### Method 2: Export from Database

1. Start the application and create your desired demo data via the UI
2. Export it using Django's dumpdata:
   ```bash
   docker exec -it django python3 manage.py dumpdata \
     auth.user \
     auctions.category \
     auctions.auction \
     auctions.pickuplocation \
     auctions.auctiontos \
     auctions.lot \
     auctions.bid \
     --indent 2 \
     --pks 9001,9002,9003,... > auctions/fixtures/demo_data.json
   ```
3. Manually adjust PKs to be in the 9000+ range if needed
4. Test by clearing the database and restarting

## Testing

Run the tests to verify the fixture loads correctly:

```bash
docker exec -it django python3 manage.py test auctions.tests.LoadDemoDataTests
```

Or manually test:

```bash
# Clear existing data (in development only!)
docker exec -it django python3 manage.py flush --no-input

# Load the fixture
docker exec -it django python3 manage.py load_demo_data
```

## Demo User Accounts

The fixture creates these demo users:
- `demo_admin` - Admin user with full permissions
- `demo_seller1` - Seller with club member status
- `demo_seller2` - Seller without club member status
- `demo_bidder1` - Bidder only (no selling permission in some auctions)

**Note**: All demo users have the same password hash (not a real password). These are for demonstration only and should not be used in production.

## Fixture Details

### Primary Keys (PKs)

All demo data uses PKs starting at 9000 to avoid conflicts:
- Users: 9001-9004
- Categories: 9001-9004
- Auctions: 9001-9003
- Pickup Locations: 9001-9007
- AuctionTOS: 9001-9011
- Lots: 90001-90013
- Bids: 90001-90009

### Auction States

1. **Demo In-Person Auction** (pk=9001)
   - Date: June 15, 2024 (past date)
   - Type: In-person
   - Has 3 lots, no online bidding

2. **Demo Online Auction - Active Now!** (pk=9002)
   - Start: Nov 25, 2024
   - End: Dec 25, 2025 (future date - **NOTE: Update periodically to keep this auction "active"**)
   - Type: Online with multiple pickup locations
   - Has 5 active lots, 1 sold via buy now
   - Includes mail shipping option

3. **Demo Ended Auction** (pk=9003)
   - Dates: Sept-Oct 2024 (ended)
   - Type: Online with multiple locations
   - Has 5 ended lots with winners
   - Shows completed auction state

**Important**: The "active" auction has a hardcoded end date of Dec 25, 2025. When this date passes, the auction will no longer appear as active. Update the `date_end` field in the fixture to a future date to keep this auction active for demonstration purposes.

### Pickup Locations

Each auction has appropriate pickup locations:
- In-person: Single main hall location
- Online auctions: Multiple physical locations + mail shipping option
- Mail locations have `pickup_by_mail=true` and no coordinates

## Troubleshooting

**Fixture won't load**: Check that:
- DEBUG=True in your environment
- No auctions exist (run `flush` first if needed)
- JSON is valid (use `python3 -m json.tool demo_data.json`)
- All referenced PKs exist (users, locations, etc.)

**Data conflicts**: Demo data uses 9000+ PKs. If you have data in that range, you may need to adjust the fixture PKs.

**Dates are wrong**: Update the date fields in the fixture to be relative to when you want to test. Active auctions should have `date_end` in the future.
