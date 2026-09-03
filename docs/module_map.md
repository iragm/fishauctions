<!-- GENERATED FILE -- do not edit by hand.
     Regenerate with: python3 auctions/module_map.py --write
     Every line below comes from a module's own docstring and top-level names, so this file cannot
     drift from the code; auctions/test_module_map.py fails the build if it has. Why it works this
     way is explained in auctions/module_map.py. -->

# Module map

One line per Python module: its first docstring line, and the top-level names it defines. This is
the "which file do I open" index. It is not documentation -- the docstring in the module is, and
this only quotes its opening sentence.


## `./`

- **`gunicorn.conf.py`** (21 lines)
  Gunicorn configuration for the ASGI (uvicorn) worker.
- **`locustfile.py`** (35 lines)
  `QuickstartUser`
- **`manage.py`** (24 lines)
  Django's command-line utility for administrative tasks.
  `main`

## `auctions/`

- **`account_deletion.py`** (473 lines)
  Account deletion — what "delete my account" means here, and the machinery that does it.
  `deletion_due_date`, `blacklist_refresh_tokens`, `request_deletion`, `cancel_deletion`, `deletion_summary`, `delete_account`, `process_due_deletions`
- **`account_nav.py`** (220 lines)
  The **Account setup** menu: which pages are in it, which one you're on, and where /account/setup/ lands.
  `Row`, `Group`, `active_page`, `remember`, `landing_url`, `groups_for`
- **`admin.py`** (1641 lines)
  The Django admin: the staff-only back door, and the handful of jobs that only live here.
- **`announcements.py`** (491 lines)
  Club announcements: one message, sent to the places a club's members actually look.
- **`app_links.py`** (153 lines)
  The two files that let a site link open in the mobile app instead of a browser.
  `assetlinks`, `apple_app_site_association`
- **`apple_notifications.py`** (514 lines)
  Sign in with Apple server-to-server notifications — Apple telling us an account changed.
  `AppleNotificationError`, `notifications_configured`, `verify_notification`, `parse_events`, `handle_event`, `process_notification`, `AppleServerNotificationView`
- **`apple_signin.py`** (203 lines)
  Sign in with Apple: redeeming the authorization code, and revoking the grant on deletion.
  `revocation_configured`, `redeem_authorization_code`, `store_tokens`, `revoke_account`, `revoke_all_for_user`
- **`apple_wallet.py`** (380 lines)
  Helpers for Apple Wallet (PassKit): .pkpass generation and pass-update pushes.
  `is_configured`, `ensure_apple_pass_auth_token`, `generate_pkpass_for_member`, `send_pass_update_notification`
- **`apps.py`** (41 lines)
  `AuctionsConfig`
- **`aquarium_species.py`** (363 lines)
  The curated aquarium-trade species list, and the strains that hang off it.
  `Row`, `Result`, `read_rows`, `kind_hints`, `load`
- **`ar_mapping.py`** (1068 lines)
  AR lot-location solver — bearing-dominant 2D bundle adjustment.
  `solve_positions`, `update_positions_for_auction`
- **`authentication.py`** (68 lines)
  `APIKeyAuthentication`, `OptionalAPIKeyAuthentication`, `ApiKeyThrottle`
- **`bidding.py`** (448 lines)
  Bidding logic for lots.
  `check_bidding_permissions`, `reset_lot_end_time`, `bid_on_lot`, `place_bid_and_broadcast`
- **`brevo.py`** (641 lines)
  One-way Django -> Brevo sync for clubs.
- **`cloudflare_images.py`** (156 lines)
  Cloudflare Images integration.
  `CloudflareImagesError`, `enabled`, `delivery_url`, `image_url`, `upload`, `delete`, `sync_variants`
- **`club_events.py`** (534 lines)
  Keeps a club's event list, its Google Calendar, and its Discord events in step.
- **`command_palette.py`** (1547 lines)
  Shared logic for the command palette.
  `resolve_page`, `app_destinations_for_prompt`, `app_deep_link_by_name`, `default_items`, `search`, `log_search`
- **`consumers.py`** (427 lines)
  The websocket half of the site: live bidding, chat, and "somebody else just bid".
  `check_chat_permissions`, `check_all_permissions`, `post_chat_message`, `broadcast_bid_result`, `LotConsumer`, `UserConsumer`, `AuctionConsumer`
- **`context_processors.py`** (228 lines)
  `google_analytics`, `google_oauth`, `theme`, `add_tz`, `add_location`, `dismissed_cookies_tos`, `site_config`, `label_print_method`, `user_clubs`, `account_nav`
- **`discord_events.py`** (355 lines)
  Discord scheduled events for clubs.
  `send_channel_message`, `delete_channel_message`, `create_scheduled_event`, `cancel_scheduled_event`, `sync_club_events`, `sync_one_event`, `sync_auction_events`
- **`donation_views.py`** (562 lines)
  Views for donation tracking: the vendor table, the vendor panel, and the contact dialog.
  `DonationPermissionMixin`, `ClubDonationVendorsView`, `ClubDonationSettingsView`, `DonationVendorPanelView`, `DonationVendorDeleteView`, `DonationContactView`, `DonationEmailPreviewView`, `DonationUnsubscribeView`, `InboundDonationEmailView`
- **`donations.py`** (957 lines)
  Donation tracking: asking vendors for donations, and reading what they write back.
- **`email_routing.py`** (163 lines)
  `email_routing_enabled`, `email_routing_domain`, `build_routed_sender_address`, `sender_with_display_name`, `admin_routing_email`, `resolve_donation_alias`, `resolve_routing_info`, `resolve_routed_recipient`
- **`error_views.py`** (35 lines)
  Error handlers that surface otherwise-swallowed tracebacks.
  `error_404`, `error_500`
- **`filters.py`** (1748 lines)
  The search and filter boxes above every table: what a query in one of them means.
- **`fishbase.py`** (76 lines)
  Where the species list comes from.
  `parquet_url`, `available_versions`
- **`forms.py`** (7089 lines)
  Every form on the site: what a person is allowed to type, and what it means when they do.
- **`geocoding.py`** (67 lines)
  Turning a typed address into a point on the map.
  `configured`, `geocode`
- **`google_calendar.py`** (780 lines)
  Two-way Google Calendar sync for clubs.
- **`google_wallet.py`** (323 lines)
  Helpers for talking to the Google Wallet REST API.
  `is_configured`, `get_access_token`, `member_text_modules`, `update_generic_object_for_member`, `expire_generic_object_for_member`, `create_generic_class`
- **`helper_functions.py`** (141 lines)
  Got sick of these being scattered all over the codebase,
  `scrub_emails`, `get_currency_symbol`, `bin_data`
- **`llm.py`** (453 lines)
  Provider abstraction for everything on this site that talks to a language model.
- **`mailchimp.py`** (735 lines)
  One-way Django -> Mailchimp sync for clubs.
- **`middleware.py`** (64 lines)
  Custom middleware for the auctions application.
  `MobileAppMiddleware`
- **`models.py`** (14975 lines)
  The database: 80 models, and the reason they are still in one file.
- **`module_map.py`** (256 lines)
  The map of this repository: which module does what, generated from the modules themselves.
  `Module`, `iter_modules`, `render`, `rule_violations`, `main`
- **`notifications.py`** (265 lines)
  Email → mobile-push routing.
  `push_configured`, `user_prefers_push`, `user_has_app_push`, `notify_user`, `send_fcm_message`, `send_fcm_data_message`
- **`palette_actions.py`** (15265 lines)
  The things the command palette's natural-language assist is allowed to do.
- **`palette_assist.py`** (1518 lines)
  Natural-language orchestration for the command palette.
- **`palette_routes.py`** (1878 lines)
  Every page on the site, as a thing the command palette's assistant can reach.
  `Route`, `excluded_reason`, `is_third_party`, `audit`, `catalog_for_prompt`, `match_routes`, `get_route`, `route_needs_an_auction`, `resolve_route`, `page_context_from_path`
- **`passkit_views.py`** (191 lines)
  Apple PassKit web service — the endpoints installed Wallet passes talk to.
  `PassKitRegistrationView`, `PassKitDeviceRegistrationsView`, `PassKitPassView`, `PassKitLogView`
- **`printer_drafts.py`** (227 lines)
  Turn a characterized :class:`~auctions.models.ObservedPrinter` into a draft printer profile.
  `DraftError`, `profile_matches_observation`, `pick_gatt_ids`, `draft_slug`, `draft_profile_from_observation`
- **`printer_programs.py`** (756 lines)
  Validation + seed data for :class:`ThermalPrinterProfile` command programs.
  `ProgramValidationError`, `validate_program`, `validate_match_patterns`, `validate_profile_programs`, `serialize_profile`
- **`printing.py`** (89 lines)
  Shared label-printing helpers.
  `deterministic_warnings`, `label_prefs_warnings`, `warning_matrix`
- **`recurrence.py`** (151 lines)
  Repeating club events.
  `clean_lines`, `to_text`, `from_text`, `current_or_next`, `with_exdate`, `describe`
- **`routing.py`** (9 lines)
- **`serializers.py`** (978 lines)
  DRF serializers for the club API: the shape of what a key gets back.
- **`services.py`** (1106 lines)
  The operations that are the same whoever asked: web page, API, app or assistant.
- **`signals.py`** (950 lines)
  Signal handlers for the auctions app.
- **`site_setup.py`** (141 lines)
  `single_club_mode_enabled`, `single_club_name`, `site_paypal_configured`, `get_server_public_ip`, `get_single_club`, `ensure_single_club_membership_for_user`
- **`social_adapter.py`** (67 lines)
  Site-specific allauth socialaccount adapter.
  `FishAuctionsSocialAccountAdapter`
- **`source_code.py`** (475 lines)
  This site's own source code, read out of the public repository it is published from.
- **`speaker_topics.py`** (218 lines)
  The speaker directory's fixed topic vocabulary.
  `canonical_topic_name`, `topic_needs_review`, `ensure_speaker_topics`
- **`species_categories.py`** (501 lines)
  Turn a species' taxonomy into one of the site's :class:`~auctions.models.Category` rows.
  `normalize_category_name`, `CategoryResolver`, `hint_for`, `assign_categories`
- **`species_matching.py`** (1185 lines)
  Turn a lot name someone typed into a short list of species to pick from.
- **`tables.py`** (1424 lines)
  The ``django_tables2`` tables behind every list on the site.
- **`tasks.py`** (2089 lines)
  Celery tasks for the auctions app.
- **`template_lint.py`** (128 lines)
  Catches Django template tags that silently render as text instead of being parsed.
  `iter_template_files`, `check_text`, `check_templates`, `main`
- **`test_account_deletion.py`** (711 lines)
  Tests for account deletion (Part D).
- **`test_account_nav.py`** (301 lines)
  The Account setup menu: `auctions/account_nav.py`, its sidebar, and /account/setup/.
  `SidebarReachTests`, `LandingTests`, `PaymentRowTests`, `NavbarTests`, `SettingsSplitTests`
- **`test_app_links.py`** (154 lines)
  Part LINKS — the two files that make a site link open in the app.
  `AppLinkFilesTests`, `AppLinksUnconfiguredTests`
- **`test_apple_notifications.py`** (645 lines)
  Tests for Sign in with Apple server-to-server notifications.
  `AppleNotificationTestCase`, `AppleNotificationVerificationTests`, `AppleNotificationForgeryTests`, `AppleNotificationEventParsingTests`, `AppleConsentRevokedTests`, `AppleAccountDeleteTests`, `AppleEmailForwardingTests`, `AppleNotificationRetryTests`, `AppleNotificationErrorTypeTests`, `AppleNotificationChecklistTests`
- **`test_ar.py`** (1800 lines)
  Tests for Part 3 — AR lot scanning & location mapping, plus the two Part 1/2 follow-up fixes.
- **`test_assistant_context.py`** (595 lines)
  What the assistant does when nobody is looking at a page.
- **`test_auction_links.py`** (591 lines)
  Auction join links, the lot list's behaviour, and the Cloudflare image pipeline.
  `AuctionJoinLinksUserTests`, `AuctionTOSEmailChangeGuardTests`, `RelinkAuctiontosUsersCommandTests`, `LotListUXTests`, `CloudflareImagesTests`
- **`test_auction_misc.py`** (631 lines)
  The smaller auction surfaces -- pickup locations, stats, bulk pages, watching, images.
- **`test_auction_props.py`** (1338 lines)
  ``Auction`` computed properties -- the many questions the rest of the site asks an auction.
  `AuctionPropertyTests`, `LotPropertyTests`, `LotInvoicePropertyTests`, `SellerInvoiceRemovedLotTests`, `BuyNowSellerCreditTests`
- **`test_auction_views.py`** (723 lines)
  Auction pages an admin edits: permissions, the edit form, custom fields and cloning.
  `AuctionViewPermissionTests`, `AuctionEditViewTests`, `AuctionCustomFieldsViewTests`, `AuctionCloneCustomFieldsTests`, `PayPalFormFieldVisibilityTests`, `LotListViewTests`, `MyLotsViewTests`, `AuctionUsersViewTests`
- **`test_auctiontos.py`** (652 lines)
  ``AuctionTOS``: the admin filter over it, feedback, and merging two participants.
  `LotAdminFilterTests`, `FeedbackTestCase`, `AuctionHistoryTestCase`, `MergeAuctionTOSTests`, `AuctionTOSMergeViewTests`
- **`test_bap_lots.py`** (1417 lines)
  The breeder award program: which lots are eligible, and the pages that award points.
- **`test_bidding.py`** (920 lines)
  Bidding: what a bid is worth, who is allowed to place one, and the refund dialog.
  `LotPricesTests`, `DecimalBidValidationTests`, `BiddingPermissionsHardeningTests`, `AuctionEditFormMinimumBidTests`, `CreateLotFormWholeDollarValidationTests`, `LotRefundDialogTests`
- **`test_bulk_add_lots.py`** (1269 lines)
  The bulk add-lots table, its per-row save, and the CSV import view.
  `BulkAddLotsAutoTests`, `UpdateAuctionStatsCommandTestCase`, `LotsByUserViewTest`, `ImportLotsFromCSVViewTests`
- **`test_cache_hygiene.py`** (133 lines)
  Guards against tests that clear a cache shared with every other parallel worker.
  `find_test_modules`, `check_source`, `CachesAreNotSharedBetweenWorkersTests`, `CacheHygieneCheckerTests`
- **`test_camera_scanner.py`** (142 lines)
  Guards the iPhone code path through the camera barcode scanner.
  `CameraScannerSourceTests`, `ScannerTemplateTests`, `QuickCheckoutCameraStartsOffTests`
- **`test_celery_tasks.py`** (1068 lines)
  Tests for Celery tasks.
  `CeleryTasksTestCase`, `SendInvoiceNotificationTaskTestCase`, `ScheduleInvoiceNotificationTestCase`, `CleanupOldInvoiceNotificationTasksTestCase`, `FixedDatabaseSchedulerTestCase`, `OverlapLockTestCase`, `YearlyBapResetTestCase`, `AuctionStatsWatchdogTestCase`, `PerItemIsolationTestCase`, `OrphanedPeriodicTaskTestCase`
- **`test_checkin.py`** (653 lines)
  Tests for Part 6 — proximity check-in & welcome (mobile ping/join/set-location).
- **`test_club_announcements.py`** (1143 lines)
  Tests for club announcements, the website-integration snippets, and the embeds behind them.
- **`test_club_api_read.py`** (1467 lines)
  The club REST API's read side: members, BAP lots, auctions and lots.
  `ClubAPITests`, `ClubAPIKeyMemberPermissionTests`, `ClubBapLotAPITests`, `ClubAuctionReadAPITests`, `ParseBoolEnvTests`, `RequireSecureProdSecretsTests`, `ClubAuctionIntegrationTests`
- **`test_club_events.py`** (3051 lines)
  Tests for club events, Google Calendar sync, and the Discord events built on top of them.
- **`test_club_ledger.py`** (1226 lines)
  The club ledger on a cash basis: what a paid invoice freezes, and how dues reverse.
  `ClubMoneyLedgerCashBasisTests`, `PaidInvoiceFreezeTests`, `InvoiceDedupeLedgerTests`, `ClubMembershipDuesReversalTests`, `MakeClubAdminAssignsAuctionsTests`, `BapTop10ChartTests`, `ClubTreasurerReportViewTests`, `ClubTreasurerOutstandingInvoiceTests`
- **`test_club_money.py`** (816 lines)
  Money into and out of a club: PayPal without OAuth, invoices, profit and seller splits.
  `NonOAuthPayPalTests`, `ClubMoneyInvoiceHistoryTests`, `ClubProfitTests`, `TotalToSellersPercentToClubTests`, `AuctionGrossTests`
- **`test_club_permissions.py`** (960 lines)
  Club permissions in the awkward cases: wildcards, dialogs, Discord admin, view-only.
  `ClubPermissionWildcardTests`, `ClubPermissionsDialogTests`, `ClubMemberDiscordAdminViewTests`, `ClubMemberManagementViewTests`, `ClubViewOnlyAccessTests`, `ClubMembershipInvoiceTests`, `ClubMembershipSettingsFormFieldsTests`, `PaymentSellerClubLinkTests`
- **`test_club_settings.py`** (686 lines)
  A club's own settings pages: BAP, general settings and email routing.
  `ClubBapSettingsViewTests`, `ClubSettingsViewTests`, `ClubEmailRoutingTests`, `RoutedSenderDisplayNameTests`, `SesSendsTheMessagesOwnFromAddressTests`, `InboundEmailRoutingAPITests`, `AuctionSlugSanitizationTests`, `AuctionEmailSenderTests`, `ClubEmailSettingsFormTests`
- **`test_club_users.py`** (1046 lines)
  Managing people through a club rather than through an auction, and the bid API.
  `ManageUsersThroughClubTests`, `PlaceBidApiTests`
- **`test_clubs.py`** (1378 lines)
  Clubs: the model, the pages, and who is allowed to do what inside one.
  `ClubModelTests`, `ClubViewTests`, `ClubPermissionTests`, `ClubMemberUpdateTests`
- **`test_csv_import.py`** (1113 lines)
  Importing lots and users from a CSV or a club's Google Drive sheet.
  `AuctionHistoryTests`, `CSVImportTests`, `CSVImportBiddingPermissionTests`, `EnableBiddingForAllUsersTests`, `CSVImportPreviewTests`, `GoogleDriveImportTests`, `WeeklyPromoEmailTrackingTestCase`
- **`test_data_leak_penetration.py`** (267 lines)
  Penetration tests to verify no data leaks from public endpoints.
  `DataLeakPenetrationTests`
- **`test_donations.py`** (1679 lines)
  Tests for donation tracking: routing, the inbound webhook, the LLM seams, and the UI gates.
- **`test_endauctions.py`** (930 lines)
  The ``endauctions`` command and the websocket layer that tells everyone what happened.
  `LotEndauctionsMethodsTests`, `WebsocketClientDisconnectTests`, `WebSocketConsumerTests`, `HasEverGrantedPermissionTests`
- **`test_helpers.py`** (1219 lines)
  The utility layer -- helper functions, model utilities, template tags, context processors.
  `HelperFunctionsTestCase`, `ModelUtilityFunctionsTestCase`, `FormsUtilityTestCase`, `TemplateTagsTestCase`, `ContextProcessorsTestCase`, `FooterIconTests`, `SiteWebmanifestTests`, `GoogleLoginTemplateVisibilityTests`, `AdminSetupChecklistViewTests`
- **`test_invoice_models.py`** (220 lines)
  Invoice models: what an invoice contains, when it is created, and when it notifies.
  `InvoiceModelTests`, `InvoiceCreateViewTests`, `InvoiceNotificationDueTests`
- **`test_lot_create.py`** (542 lines)
  Creating a lot, and the invoice lists a seller and buyer see afterwards.
  `LotCreateViewTests`, `InvoiceViewTests`, `MyInvoicesListTests`
- **`test_lot_images.py`** (688 lines)
  Lot images: uploading, ordering, rotating and deleting them; plus signup forms.
  `LotImageManagementTests`, `ChangeUsernameFormTest`, `CustomSignupFormTest`, `AdminUserSignupsJSONTests`
- **`test_lot_models.py`** (939 lines)
  Lot and auction model behaviour, and the chat subscriptions hanging off a lot.
  `ViewLotTest`, `AuctionModelTests`, `LotModelTests`, `LotModelConcurrencyTests`, `ChatSubscriptionTests`
- **`test_lot_page_views.py`** (371 lines)
  The two page-view history modals: one lot's, and every lot on the selling dashboard.
  `PageViewHistoryHelperTests`, `LotPageViewHistoryViewTests`, `SellingDashboardPageViewHistoryTests`
- **`test_lot_views.py`** (1042 lines)
  The lot pages an auction is actually run from: labels, push, set-winner and the queue.
  `LotLabelViewTestCase`, `UpdateLotPushNotificationsViewTestCase`, `LotPushTestNotificationViewTestCase`, `ViewLotSimpleTestCase`, `DynamicSetLotWinnerViewTestCase`, `LotQueueViewTestCase`, `AlternativeSplitLabelTests`
- **`test_marketing.py`** (848 lines)
  Mailchimp and Brevo: syncing members, webhooks, self-service and what gets redacted.
- **`test_mcp.py`** (1503 lines)
  Tests for the MCP tool catalogue.
- **`test_mcp_permissions.py`** (694 lines)
  Every tool on ``/mcp/``, pointed at somebody else's club and somebody else's auction.
  `secrets`, `CrossTenantTestCase`, `NobodyElsesDataTests`, `NobodyElsesRowsTests`, `NothingCrashesInsteadOfRefusingTests`, `PrintLabelsByPrimaryKeyTests`, `AuctionSetupBelongsToTheAuctionTests`, `ClubSetupBelongsToTheClubTests`
- **`test_mcp_resources.py`** (308 lines)
  The addressable reads and the recipes: ``resources/templates/list``, ``prompts/*``, completions.
  `ResourceCatalogueTests`, `ResourceEndpointTests`, `PromptTests`, `PromptEndpointTests`
- **`test_mcp_widgets.py`** (208 lines)
  Tests for the MCP-app widgets — the ``ui://`` resources a host renders instead of the JSON.
  `BundleTests`, `CatalogueTests`, `DocumentTests`, `ResourceEndpointTests`
- **`test_membership_flow.py`** (1332 lines)
  Club membership as money: invoices, discounts, renewals and the confirmation emails.
  `InvoiceStatusButtonTests`, `ClubMembershipRenewalFlowTests`, `PayPalSubscriptionWebhookTests`, `ClubMemberDiscountTests`, `ClubMoneyRenewalConsistencyTests`, `ClubMembershipEmailTaskTests`, `ClubBarcodeViewTests`, `QuickCheckoutHTMXTests`
- **`test_mobile_features.py`** (2650 lines)
  Tests for the mobile-app web-side features.
- **`test_mobile_last_used.py`** (156 lines)
  Tests for GET /api/mobile/auctions/last-used/ — the command palette's AR-gating lookup.
  `MobileLastUsedAuctionTests`
- **`test_mobile_menu.py`** (325 lines)
  The app's navigation drawer: /api/mobile/config/ -> "menu".
  `MenuPayloadTests`, `RowSanitizerTests`, `NavbarDriftTests`, `ConfigEndpointTests`
- **`test_mobile_offline.py`** (591 lines)
  Tests for the mobile offline-mode backend (in-person sale).
  `MobileOfflineSnapshotTests`, `MobileOfflineSyncTests`
- **`test_mobile_payments.py`** (693 lines)
  Tap to Pay from inside the app: confirming a payment, and which Square seller it routes to.
  `MobilePaymentConfirmTests`, `MobilePaymentEndpointTests`, `SquareSellerRoutingTests`, `SquareTokenHandoutAuditTests`
- **`test_mobile_social_auth.py`** (901 lines)
  Tests for native social sign-in (Sign in with Apple / Google / Facebook) — SOCIAL-0..8.
- **`test_models_misc.py`** (1239 lines)
  Model methods, signal behaviour, and the management commands that email people.
  `ModelMethodsTestCase`, `SignalLogicTestCase`, `DuplicateAuctionTOSTests`, `AuctionNoShowURLEncodingTest`, `WeeklyPromoManagementCommandTests`, `AuctionTOSNotificationsCommandTests`
- **`test_module_map.py`** (125 lines)
  Guards the module map: that it is current, and that the modules it reads are worth reading.
  `ModuleMapIsCurrentTests`, `ModuleRulesTests`, `RuleCheckerTests`, `SummaryTests`
- **`test_page_view_dedupe.py`** (119 lines)
  `remove_duplicate_views`, which used to corrupt the data it was cleaning up.
  `DeduplicationTests`
- **`test_palette_account.py`** (866 lines)
  The rest of the account, and the auction and club setup pages behind it.
- **`test_palette_assist.py`** (3895 lines)
  Tests for the command palette's natural-language assist.
- **`test_palette_core.py`** (1235 lines)
  The command palette itself, and the mobile surfaces that call into it.
  `CommandPaletteTests`, `MobileCommandPaletteTests`, `MobileMyClubsTests`, `MobileLabelTests`, `MobileConfigTests`, `FirebaseClientConfigParsingTests`, `SingleLotLabelPngTests`, `MobileEmailLoginTests`, `MobileWebSessionTests`
- **`test_palette_mic.py`** (101 lines)
  Guards for the command palette's microphone, which no other test can reach.
  `mic_branches`, `PaletteMicSourceTests`
- **`test_palette_routes.py`** (165 lines)
  Tests for the palette's page catalog.
  `RouteAuditTests`, `RouteMatchingTests`, `PageContextTests`
- **`test_palette_skills.py`** (3304 lines)
  Tests for what the command palette assistant can *do*.
- **`test_paypal.py`** (939 lines)
  PayPal: the webhooks, their event handlers, refund idempotency and the CSV export.
  `PayPalWebhookViewTests`, `PayPalWebhookEventHandlerTests`, `RefundWebhookIdempotencyTests`, `SquarePaymentUpdatedRefundResurrectionTests`, `PayPalCSVExportTests`
- **`test_remote_print.py`** (514 lines)
  Part R — printing from a computer to the phone's Bluetooth label printer.
- **`test_security.py`** (314 lines)
  Security tests to ensure AuctionTOS and user data is properly protected.
  `AuctionTOSSecurityTestCase`
- **`test_site_config.py`** (782 lines)
  Site-wide configuration: currency, email fields, locations, demo data and defaults.
  `CurrencyCustomizationTests`, `AuctionEmailFieldsTest`, `UserLocationUpdateTests`, `LoadDemoDataTests`, `EnsureSiteDefaultsCommandTests`, `AdminReadonlyFieldsTests`
- **`test_source_code.py`** (324 lines)
  The source code reader: what it can serve, and -- much more to the point -- what it cannot.
  `FakeResponse`, `fake_get`, `SourceTestCase`, `RepositorySettingTests`, `TheArchiveIsTheAllowlistTests`, `ReadingTests`, `ContentSearchTests`, `ReadSourceToolTests`
- **`test_speakers.py`** (1380 lines)
  Tests for the speaker directory: the NEC WordPress import, NEC-only scoping, the
- **`test_species.py`** (5056 lines)
  Tests for scientific names on lots: matching, the picker, labels, and genus BAP points.
- **`test_square.py`** (946 lines)
  Square: taking a payment, refunding one, the OAuth grant, and webhook signatures.
  `SquarePaymentTests`, `SquareRefundFormTests`, `SquarePaymentSuccessViewTests`, `SquareOAuthRevocationTests`, `SquareWebhookSignatureValidationTests`
- **`test_stats.py`** (1392 lines)
  The numbers on an auction's stats page, and the invoice wording that quotes them.
- **`test_support.py`** (42 lines)
  Support code shared by the test modules. Holds no tests of its own.
  `isolated_cache`
- **`test_support_page.py`** (269 lines)
  Part SUPPORT — /support/, and a way to reach a human that works with no account.
  `SupportUrlWorksSignedOutTests`, `SupportPageIsTheHelpPageTests`, `OldContactUrlStillWorksTests`, `VideoEmbedFitsItsContainerTests`, `SupportFormDeliveryTests`, `SupportFormSignedInTests`
- **`test_tap_to_pay.py`** (1325 lines)
  Tests for the Tap to Pay on iPhone review-guide work (TTP-1..4).
- **`test_template_hygiene.py`** (82 lines)
  Guards against template tags that render as text instead of being parsed.
  `TemplateTagsAreParseableTests`, `TemplateLintTests`
- **`test_user_features.py`** (518 lines)
  Preferences that change what a user sees: distance units, exports, and the trust system.
  `DistanceUnitTests`, `PayPalInfoViewTests`, `UserExportTests`, `UserTrustSystemTests`, `WatchOrUnwatchViewTests`
- **`test_userdata.py`** (299 lines)
  ``UserData`` and ``AuctionTOS`` properties, and merging one user into another.
  `AuctionTOSPropertyTests`, `UserDataPropertyTests`, `UserDataMergeIntoTests`
- **`test_voice.py`** (770 lines)
  Voice-driven set winners.
  `VoiceV1RemovedTests`, `VoiceVocabularyTests`, `VoiceVocabularyClubManagedTests`, `VoiceConfigBlockTests`, `VoicePageTests`, `VoiceCommandLogTests`, `VoiceUnmatchedLogTests`, `VoiceLogAdminTests`, `VoiceSettingsPanelTests`, `PriceAnchorCanonicalWordTests`
- **`test_volunteers.py`** (268 lines)
  Tests for Part 7 — recruit volunteers (web feature).
  `VolunteerBase`, `VolunteerPageGatingTests`, `VolunteerHelperCountTests`, `VolunteerCreateTests`, `VolunteerSignupTests`, `VolunteerPageWarningTests`
- **`test_wallet_passes.py`** (1119 lines)
  Membership cards: Google Wallet, Apple Wallet, PassKit and the numbers on them.
  `DiscordJoinModalNameTests`, `DiscordJoinButtonTests`, `ClubMemberNameModelTests`, `ClubMemberIngestNameTests`, `GoogleWalletClassCreateTests`, `MembershipNumberUniquenessTests`, `AppleWalletPassTests`, `PassKitWebServiceTests`, `MembershipNumberModeTests`, `ClubIconWalletTests`
- **`test_wallet_status.py`** (1353 lines)
  Wallet status text, error-page logging, and the label-printing surfaces in the app.
- **`tests.py`** (405 lines)
  The shared test fixture, and the helpers every other test module builds on.
  `patch_views`, `WritableMediaRoot`, `CsvImportTestMixin`, `StandardTestCase`, `SuiteStaysFastTests`
- **`tests_selenium.py`** (1045 lines)
  Selenium-based browser tests for client-side JavaScript functionality.
- **`urls.py`** (1306 lines)
  Every URL on the site, and the one place a new one has to be declared.
- **`validators.py`** (19 lines)
  `validate_username_no_at_symbol`
- **`voice.py`** (303 lines)
  Voice-driven set winners: the grammar the mobile app listens with.
  `default_anchors`, `default_number_words`, `default_homophones`, `default_weights`, `default_thresholds`, `log_command`, `log_unmatched`, `serialize_grammar`, `page_config`

## `auctions/management/`


## `auctions/management/commands/`

- **`assign_auction_to_club.py`** (127 lines)
  `Command`
- **`auction_emails.py`** (344 lines)
  The nightly email about auctions worth knowing about, and the Discord post beside it.
  `Command`
- **`auctiontos_notifications.py`** (220 lines)
  `send_tos_notification`, `Command`
- **`backfill_bap_reasons.py`** (125 lines)
  `Command`
- **`backfill_lot_species.py`** (597 lines)
  Attach a species to the lots that were sold before there was a species list to pick from.
  `group_key`, `NameGroup`, `Command`
- **`backfill_lot_users.py`** (86 lines)
  `Command`
- **`change_assistant.py`** (29 lines)
  `Command`
- **`change_paypal.py`** (28 lines)
  `Command`
- **`change_square.py`** (28 lines)
  `Command`
- **`change_standalone_lots.py`** (28 lines)
  `Command`
- **`check_apple_wallet.py`** (91 lines)
  Diagnose the Apple Wallet signing setup end to end.
  `Command`
- **`deduplicate_user_interest.py`** (29 lines)
  `Command`
- **`delete_pending_accounts.py`** (18 lines)
  Delete accounts whose deletion grace period has expired.
  `Command`
- **`email_invoice.py`** (42 lines)
  `Command`
- **`email_unseen_chats.py`** (53 lines)
  `Command`
- **`empty_account_and_move_data.py`** (35 lines)
  `Command`
- **`endauctions.py`** (143 lines)
  `declare_winners_on_lots`, `deactivate_pretty_much_over_lots`, `Command`
- **`ensure_site_defaults.py`** (71 lines)
  `Command`
- **`ensure_speaker_topics.py`** (20 lines)
  Create the speaker directory's fixed topic vocabulary.
  `Command`
- **`find_square_reconnects.py`** (46 lines)
  `Command`
- **`geocode_speakers.py`** (196 lines)
  Backfill speaker locations that the NEC WordPress export didn't carry.
  `Command`
- **`import_fishbase.py`** (549 lines)
  Load the species picklist from a pinned FishBase snapshot, plus the curated aquarium list.
  `Command`
- **`import_nec_speakers.py`** (420 lines)
  Import the Northeast Council's speaker database from a WordPress WXR export.
  `clean_text`, `Command`
- **`load_demo_data.py`** (86 lines)
  Management command to load demo data for development environments.
  `Command`
- **`migrate_to_cloudflare_images.py`** (124 lines)
  Move locally stored images to Cloudflare Images.
  `Command`
- **`mine_palette_shortcuts.py`** (186 lines)
  Turn recurring assistant answers into zero-token shortcuts.
  `Command`
- **`promo_push_notifications.py`** (123 lines)
  Push notifications promoting nearby auctions to app users who opted into push.
  `Command`
- **`purge_bot_users.py`** (19 lines)
  `Command`
- **`register_discord_commands.py`** (138 lines)
  `Command`
- **`relink_auctiontos_users.py`** (92 lines)
  `Command`
- **`remove_duplicate_views.py`** (58 lines)
  `Command`
- **`sendnotifications.py`** (65 lines)
  `Command`
- **`set_user_location.py`** (164 lines)
  `Command`
- **`setup_celery_beat.py`** (128 lines)
  Management command to set up Celery Beat periodic tasks in the database.
  `Command`
- **`split_speaker_talks.py`** (178 lines)
  Recover the individual talk titles from an imported speaker's run-on "Programs:" list.
  `normalize`, `split_is_faithful`, `Command`
- **`sync_google_wallet_classes.py`** (104 lines)
  `Command`
- **`tap_to_pay_launch_announcement.py`** (184 lines)
  The Tap to Pay on iPhone launch announcement — Apple marketing requirements 6.1 and 6.3.
  `Command`
- **`update_ar_positions.py`** (49 lines)
  Re-solve AR lot positions for auctions with fresh sightings, and prune the observation buffer.
  `Command`
- **`update_auction_stats.py`** (30 lines)
  `Command`
- **`update_breederboard.py`** (110 lines)
  `Command`
- **`update_user_interest.py`** (32 lines)
  `updateInterest`, `Command`
- **`webpush_notifications_deduplicate.py`** (19 lines)
  `Command`
- **`weekly_promo.py`** (249 lines)
  `Command`

## `auctions/mcp/`

The site's Model Context Protocol server, and the tool catalogue behind it.

- **`auth.py`** (354 lines)
  Who is calling ``/mcp/``, and what they may do.
- **`cimd.py`** (76 lines)
  Client ID Metadata Document handling for the clients that actually turn up.
  `supported_grant_types`, `narrow_grant_types`, `ClientMetadataFetcher`
- **`icons.py`** (154 lines)
  Icons for the tools, the prompts, the resources and the server itself.
  `domain`, `absolute`, `icons`, `for_action`, `for_prompt`, `for_uri`, `server`
- **`prompts.py`** (270 lines)
  Prompts: the recipes, offered to the *person* rather than to the model.
  `Argument`, `Prompt`, `descriptors`, `prompt_list`, `render`, `complete`, `completes`
- **`protocol.py`** (329 lines)
  JSON-RPC 2.0 and the MCP methods, with no HTTP in it.
  `Caller`, `error`, `is_notification`, `negotiate`, `handle`
- **`resources.py`** (406 lines)
  Addressable reads: the same answers the read-only tools give, reachable by URI.
  `Template`, `template_descriptors`, `fixed_descriptors`, `match`, `read`, `links_for`
- **`tools.py`** (535 lines)
  The action registry, as MCP tools.
- **`transport.py`** (173 lines)
  The HTTP end of the MCP server: one view, at ``/mcp/``.
  `MCPEndpointView`
- **`widgets.py`** (235 lines)
  Interactive views this server publishes as MCP-app widgets.
  `resource_descriptors`, `read_resource`, `tool_meta`

## `auctions/mobile/`

- **`authentication.py`** (45 lines)
  Authentication classes for mobile endpoints.
  `OptionalJWTAuthentication`
- **`menu.py`** (196 lines)
  The app's navigation drawer, built here and served in /api/mobile/config/.
  `menu_for`
- **`permissions.py`** (17 lines)
  `IsMobileAuthenticated`
- **`renderers.py`** (50 lines)
  DRF renderers for mobile endpoints that return raw bytes.
  `BinaryRenderer`, `PdfRenderer`, `PngRenderer`
- **`serializers.py`** (600 lines)
  Request and response shapes for the mobile app's own API.
- **`urls.py`** (133 lines)
- **`views.py`** (2443 lines)
  Mobile API views.

## `auctions/mobile/services/`

- **`ar.py`** (440 lines)
  AR lot-scanning service — overlay/card metadata, observation ingestion, and position payloads.
  `ar_dirty_key`, `mark_auction_dirty`, `drain_dirty_auction_pks`, `locatable_auction_pks`, `build_lot_metadata`, `ingest_observations`, `record_ar_events`, `positions_payload`, `clear_positions`
- **`auth.py`** (60 lines)
  `MobileAuthService`
- **`checkin.py`** (313 lines)
  Proximity check-in & welcome service.
  `evaluate_ping`, `join_auction`, `set_auction_location`
- **`devices.py`** (89 lines)
  `DeviceService`
- **`label_pdf.py`** (51 lines)
  Single-lot label PDF for the mobile ``fishauctions://print/<pk>`` deep link.
  `render_single_lot_pdf`
- **`label_raster.py`** (82 lines)
  Rasterize the label PDF so the Bluetooth PNG *is* the PDF.
  `rasterize_pdf`, `render_lot_label_png`
- **`label_renderers.py`** (181 lines)
  Fallback label rendering for the mobile app.
  `LabelRenderer`, `PngLabelRenderer`, `get_renderer`, `supported_formats`
- **`labels.py`** (109 lines)
  `LabelService`
- **`offline.py`** (552 lines)
  Offline-mode service for the mobile app's in-person sale screens.
  `get_last_admin_auction`, `build_snapshot`, `apply_ops`
- **`payments.py`** (758 lines)
  Taking a card payment in the room, through the app's Tap to Pay.
  `PaymentVerificationError`, `PaymentAlreadyChargedError`, `TapToPayAttemptOpen`, `SquareReconnectRequired`, `PaymentService`
- **`printers.py`** (138 lines)
  Recording which Bluetooth printers users actually pair, and how they were identified.
  `record_observation`
- **`remote_print.py`** (159 lines)
  Printing from a computer to the phone's Bluetooth label printer.
  `heartbeat`, `can_print_from_computer`, `create_job`, `dispatch`, `start`, `job_state`
- **`social_auth.py`** (444 lines)
  Native social sign-in for the mobile app: verify a provider credential, then let allauth decide.
  `SocialAuthError`, `build_sociallogin`, `PendingSocialLogin`, `resolve_completed_user`
- **`voice.py`** (106 lines)
  The vocabulary the app matches spoken words against, for one auction.
  `lot_numbers`, `bidder_numbers`, `build_vocabulary`
- **`web_session.py`** (91 lines)
  `mark_session_opened_by_app`, `session_opened_by_app`, `WebSessionService`

## `auctions/templatetags/`

- **`bap_filters.py`** (9 lines)
  `get_attr`
- **`club_nav_tags.py`** (97 lines)
  `club_sidebar`
- **`currency_filters.py`** (51 lines)
  `currency_symbol`, `format_price`
- **`distance_filters.py`** (70 lines)
  `convert_distance`, `distance_display`
- **`membership_tags.py`** (120 lines)
  `membership_barcode`, `google_wallet_save_url`
- **`species_tags.py`** (11 lines)
  `fishbase_citation`

## `auctions/views/`

Every view on the site, split by what part of it the view belongs to.

- **`account.py`** (578 lines)
  The reader's own account: profile, username, preferences, notifications, deletion.
- **`admin_checklist.py`** (1024 lines)
  The admin setup checklist: the one page that says what a new site still needs.
  `AdminSetupChecklistView`
- **`ajax.py`** (744 lines)
  The small endpoints the pages call, rather than the pages themselves.
- **`auction_admin.py`** (1226 lines)
  Setting an auction up, and running the room: pickup locations, users, check-in.
- **`auction_extras.py`** (644 lines)
  The rest of an auction's admin surface: label config, bulk printing, no-shows, chat.
- **`auction_pages.py`** (1057 lines)
  The auction as a thing you join: the TOS, creating one, and the auction's own page.
  `AuctionTOSDelete`, `AuctionTOSAdmin`, `AuctionConfirmView`, `AuctionCreateView`, `AuctionInfo`
- **`auction_stats.py`** (1206 lines)
  The JSON behind the charts on one auction's stats page.
- **`bap.py`** (621 lines)
  The breeder award program: settings, overrides, awards and the lots behind them.
- **`base.py`** (1087 lines)
  Shared machinery for every view on the site: the mixins that decide who may see a page.
- **`browse.py`** (832 lines)
  The lot lists people browse, and what they do to a lot without opening it.
- **`bulk_actions.py`** (471 lines)
  The bulk buttons on the auction admin pages: mark paid, set won, enable bidding.
  `GetClubs`, `BulkSetLotsWon`, `InvoiceBulkUpdateStatus`, `MarkInvoicesReady`, `MarkInvoicesPaid`, `EnableBiddingForAllUsers`, `LotRefundDialog`
- **`bulk_add.py`** (939 lines)
  Getting people in at once: bulk add users, and a club's shared spreadsheet.
  `CSVContactImportMixin`, `BulkAddUsers`, `ImportFromGoogleDrive`
- **`bulk_add_lots.py`** (972 lines)
  Getting lots in at once: the bulk table, the quick-add page, and the CSV importer.
  `BulkAddLots`, `BulkAddLotsAuto`, `SaveLotAjax`, `ImportLotsFromCSV`
- **`club_admin.py`** (1049 lines)
  Setting a club up: its details, membership settings, payment accounts, email.
- **`club_api.py`** (1342 lines)
  The club REST API: ``/api/v1/clubs/<slug>/…``.
- **`club_api_keys.py`** (424 lines)
  Club API keys, and the page that documents the API they open.
  `ClubAPIKeyListView`, `ClubAPIKeyCreateView`, `club_api_documentation_context`, `ClubAPIKeyDetailView`, `ClubAPIKeyRevokeView`, `ClubAPIKeyFieldMapCreateView`, `ClubAPIKeyFieldMapDeleteView`, `ClubMemberMapView`, `SelfServeContactLinkView`
- **`club_integrations.py`** (1317 lines)
  The outside accounts a club connects: Mailchimp, Brevo, Google Calendar, Square links.
- **`club_members.py`** (1063 lines)
  The club's list of people: joining, renewing, permissions, cards.
- **`club_pages.py`** (587 lines)
  A club's public page, and the two links that identify a member on it.
  `ClubDetailView`, `ClubMemberByUUIDView`, `ClubMemberByNumberView`, `ClubAdminView`
- **`club_reports.py`** (814 lines)
  What a club's officers read: history, stats, the treasurer's report, money in and out.
  `ClubHistoryView`, `ClubStatsView`, `ClubTreasurerReportView`, `ClubTreasurerReportExportView`, `ClubMoneyCreateView`, `ClubMoneyBalanceView`, `ClubMemberCSVImportView`, `ClubMemberCSVExportView`
- **`discord.py`** (978 lines)
  Discord: verifying its signatures, answering its interactions, and syncing roles.
  `InboundEmailRoutingView`, `verify_discord_signature`, `assign_discord_role`, `DiscordInteractionsView`, `LotBapPointsView`, `ClubDiscordConfigView`, `ClubDiscordFetchRolesView`, `ClubDiscordEditRoleView`, `ClubDiscordSetDefaultRoleView`, `ClubDiscordSendJoinMessageView`
- **`embeds.py`** (693 lines)
  The snippets a club puts on its own website, and the pages behind them.
- **`exports.py`** (887 lines)
  Taking data back out: the CSV exports, the reports, and the mailing list.
- **`invoices.py`** (377 lines)
  Invoices as a person reads them: the list, one invoice, and the no-login link.
  `Invoices`, `InvoiceCreateView`, `InvoiceView`, `InvoiceNoLoginView`, `SquarePaymentSuccessView`
- **`lot_pages.py`** (1419 lines)
  One lot: its page, its photos, and creating or editing it.
- **`palette.py`** (580 lines)
  The command palette and the assistant surface behind it.
- **`payments.py`** (1152 lines)
  Connecting a club's PayPal and Square accounts, and taking a payment through them.
- **`printing.py`** (616 lines)
  Labels: what gets drawn on them, and getting them to a printer.
  `LotLabelView`, `UnprintedLotLabelsView`, `SingleLotLabelView`, `RemotePrintJobMixin`, `RemotePrintJobStatusView`, `RemotePrintJobRetryView`, `RemotePrintJobCancelView`
- **`selling.py`** (1053 lines)
  Auction night: setting winners, the lot queue, and the volunteers who help.
- **`site_admin.py`** (744 lines)
  The superuser's dashboard: traffic, signups, referrers, the user flow map.
- **`site_pages.py`** (603 lines)
  Pages that belong to the site rather than to any auction or club.
- **`speakers.py`** (517 lines)
  The speaker directory: who will come and talk to a club, and what about.
  `NECSpeakerAccessMixin`, `SpeakerListView`, `SpeakerPanelView`, `SpeakerDetailView`, `SpeakerCreateView`, `SpeakerUpdateView`, `SpeakerDeleteView`, `SpeakerTagView`, `SpeakerCommentView`, `SpeakerCommentDeleteView`
- **`species.py`** (616 lines)
  Adding species and common names, and the superuser's queue for cleaning them up.
- **`webhooks.py`** (1016 lines)
  What PayPal, Square and the email provider send us, unprompted.
  `PayPalWebhookView`, `PayPalSubscriptionWebhookView`, `SquareWebhookView`, `QuickCheckout`, `QuickCheckoutHTMX`

## `fishauctions/`

This will make sure the app is always imported when

- **`_env.py`** (81 lines)
  Helpers for parsing environment variables in settings.
  `parse_bool_env`, `require_secure_prod_secrets`, `env_has_real_value`
- **`asgi.py`** (68 lines)
  `LogWebsocketExceptions`
- **`asgi_old.py`** (25 lines)
  ASGI config for fishauctions project.
- **`celery.py`** (234 lines)
  Celery configuration for fishauctions project.
  `start_auction_stats_task`, `start_bap_recalculation_tasks`, `debug_task`
- **`custom_scheduler.py`** (78 lines)
  Custom Celery Beat Scheduler to work around django-celery-beat 2.8.1 bug.
  `FixedDatabaseScheduler`
- **`firebase_config.py`** (88 lines)
  Parse the public Firebase client-config files that ship with the mobile build.
  `load_android_config`, `load_ios_config`, `load_firebase_client_config`
- **`settings.py`** (1349 lines)
  Django settings for fishauctions project.
- **`test_runner.py`** (57 lines)
  The test runner, which exists to swap the password hasher out.
  `use_fast_hashers`, `FastParallelTestSuite`, `FastTestRunner`
- **`urls.py`** (88 lines)
- **`uvicorn_worker.py`** (15 lines)
  Custom gunicorn worker that runs uvicorn on the stdlib asyncio loop.
  `AsyncioUvicornWorker`
- **`wsgi.py`** (16 lines)
  WSGI config for fishauctions project.
