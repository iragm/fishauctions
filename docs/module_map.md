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

- **`gunicorn.conf.py`** (15 lines)
  Gunicorn configuration for the ASGI (uvicorn) worker.
- **`locustfile.py`** (35 lines)
  `QuickstartUser`
- **`manage.py`** (24 lines)
  Django's command-line utility for administrative tasks.
  `main`

## `auctions/`

- **`account_deletion.py`** (390 lines)
  Account deletion: what "delete my account" means here, and the machinery for it.
  `deletion_due_date`, `blacklist_refresh_tokens`, `request_deletion`, `cancel_deletion`, `deletion_summary`, `delete_account`, `process_due_deletions`
- **`account_nav.py`** (199 lines)
  The **Account setup** menu: which pages are in it, which one you're on, and where /account/setup/ lands.
  `Row`, `Group`, `active_page`, `remember`, `landing_url`, `groups_for`
- **`admin.py`** (1566 lines)
  The Django admin: staff-only, and the few jobs that only live here.
- **`admin_paginator.py`** (50 lines)
  Paginate the admin's biggest changelists without counting the whole table.
  `EstimatedCountPaginator`
- **`admin_performance.py`** (157 lines)
  Two rules that keep an admin change page from querying once per row it shows.
  `FlatInline`, `every_admin`, `use_lookup_widgets`
- **`ads_admin.py`** (122 lines)
  The advertising admin: campaign groups, the campaigns in one, and what they cost to show.
  `AdCampaignResponseInline`, `AdCampaignInline`, `AdCampaignAdmin`, `AdCampaignGroupAdmin`
- **`announcements.py`** (403 lines)
  Club announcements: one message, sent to the places a club's members look.
- **`app_links.py`** (127 lines)
  The two files that let a site link open in the mobile app instead of a browser.
  `assetlinks`, `apple_app_site_association`
- **`apple_notifications.py`** (391 lines)
  Sign in with Apple server-to-server notifications.
  `AppleNotificationError`, `notifications_configured`, `verify_notification`, `parse_events`, `handle_event`, `process_notification`, `AppleServerNotificationView`
- **`apple_signin.py`** (194 lines)
  Sign in with Apple: redeeming the authorization code, and revoking the grant on deletion.
  `revocation_configured`, `redeem_authorization_code`, `store_tokens`, `revoke_account`, `revoke_all_for_user`
- **`apple_wallet.py`** (333 lines)
  Apple Wallet (PassKit): .pkpass generation and pass-update pushes.
  `is_configured`, `ensure_apple_pass_auth_token`, `generate_pkpass_for_member`, `send_pass_update_notification`
- **`apps.py`** (41 lines)
  `AuctionsConfig`
- **`aquarium_species.py`** (277 lines)
  The curated aquarium-trade species list (plants, invertebrates, live food, cultivars), and the
  `Row`, `Result`, `read_rows`, `kind_hints`, `load`
- **`ar_mapping.py`** (945 lines)
  AR lot-location solver: bearing-dominant 2D bundle adjustment.
  `solve_positions`, `update_positions_for_auction`
- **`auction_form_layout.py`** (220 lines)
  The layout of ``AuctionEditForm``: what an organizer sees first, and what is behind *Advanced*.
  `build_layout`, `advanced_fields_in_use`
- **`authentication.py`** (68 lines)
  `APIKeyAuthentication`, `OptionalAPIKeyAuthentication`, `ApiKeyThrottle`
- **`bidding.py`** (420 lines)
  Bidding logic for lots.
  `check_bidding_permissions`, `reset_lot_end_time`, `bid_on_lot`, `place_bid_and_broadcast`
- **`brevo.py`** (570 lines)
  One-way Django -> Brevo sync for clubs, built like auctions/mailchimp.py.
- **`cloudflare_cache.py`** (70 lines)
  Purging a file out of Cloudflare's edge cache.
  `enabled`, `purge_urls`
- **`cloudflare_images.py`** (154 lines)
  Cloudflare Images integration.
  `CloudflareImagesError`, `enabled`, `delivery_url`, `image_url`, `upload`, `delete`, `sync_variants`
- **`club_events.py`** (460 lines)
  Keeps a club's event list, its Google Calendar, and its Discord events in step.
- **`club_health.py`** (390 lines)
  Whether a club is still running auctions here, judged against its own cadence.
- **`club_import.py`** (231 lines)
  Getting a list of aquarium clubs onto this site from a curated CSV.
  `ImportedClub`, `IngestReport`, `domain_of`, `is_a_club_host`, `read_csv`, `find_existing`, `ingest`
- **`club_matching.py`** (228 lines)
  Which club does this belong to? Name normalisation, initialisms, and the auction backlog.
  `normalize`, `initials`, `derived_abbreviation`, `similarity`, `is_hand_written`, `best_match`, `Suggestion`, `suggest_clubs`
- **`command_palette.py`** (1442 lines)
  Shared logic for the command palette, behind the JSON views.
  `resolve_page`, `app_destinations_for_prompt`, `app_deep_link_by_name`, `default_items`, `search`, `log_search`
- **`consumers.py`** (410 lines)
  The websocket half of the site: live bidding, chat, and "somebody else just bid".
  `check_chat_permissions`, `check_all_permissions`, `post_chat_message`, `broadcast_bid_result`, `LotConsumer`, `UserConsumer`, `AuctionConsumer`
- **`context_processors.py`** (306 lines)
  Values every template needs and no view should have to pass.
- **`discord_events.py`** (341 lines)
  Discord scheduled events for clubs.
  `send_channel_message`, `delete_channel_message`, `create_scheduled_event`, `cancel_scheduled_event`, `sync_club_events`, `sync_one_event`, `sync_auction_events`
- **`dmca.py`** (227 lines)
  The DMCA designated agent, the takedown, and the repeat-infringer policy.
  `agent`, `is_configured`, `agent_email`, `strike_count`, `record_strike`, `take_down`, `terminate`
- **`donation_views.py`** (527 lines)
  Donation tracking views: the vendor table, the vendor panel, and the contact dialog.
  `DonationPermissionMixin`, `ClubDonationVendorsView`, `ClubDonationSettingsView`, `DonationVendorPanelView`, `DonationVendorDeleteView`, `DonationContactView`, `DonationEmailPreviewView`, `DonationUnsubscribeView`, `InboundDonationEmailView`
- **`donations.py`** (822 lines)
  Donation tracking: asking vendors for donations and reading their replies.
- **`email_routing.py`** (174 lines)
  `email_routing_enabled`, `email_routing_domain`, `build_routed_sender_address`, `sender_with_display_name`, `admin_routing_email`, `resolve_donation_alias`, `resolve_routing_info`, `resolve_routed_recipient`
- **`error_views.py`** (35 lines)
  Error handlers that surface otherwise-swallowed tracebacks.
  `error_404`, `error_500`
- **`field_adoption.py`** (208 lines)
  Which settings has anybody ever changed, reconstructed from the rows rather than a changelog.
  `FieldAdoption`, `model_field_default`, `form_field_names`, `history_edit_counts`, `field_adoption`, `auction_field_adoption`
- **`filters.py`** (1793 lines)
  The search and filter boxes above every table.
- **`fishbase.py`** (58 lines)
  Where the species list comes from.
  `parquet_url`, `available_versions`
- **`form_friction.py`** (169 lines)
  The view mixin that writes :class:`auctions.friction_models.FormFailure` rows.
  `error_codes`, `abandon_token`, `read_abandon_token`, `FormFrictionMixin`
- **`forms.py`** (6116 lines)
  Every form on the site.
- **`friction_models.py`** (74 lines)
  Where people get stuck: one row per rejected form submission.
  `FormFailure`
- **`geocoding.py`** (67 lines)
  Turning a typed address into a point on the map.
  `configured`, `geocode`
- **`google_calendar.py`** (671 lines)
  Two-way Google Calendar sync for clubs.
- **`google_wallet.py`** (302 lines)
  Helpers for the Google Wallet REST API.
  `is_configured`, `get_access_token`, `member_text_modules`, `update_generic_object_for_member`, `expire_generic_object_for_member`, `create_generic_class`
- **`helper_functions.py`** (135 lines)
  Small helpers with no home of their own: email scrubbing, currency symbols, histogram bins.
  `scrub_emails`, `get_currency_symbol`, `bin_data`
- **`history.py`** (148 lines)
  What an edit changed, in a form a query can answer.
  `is_secret_field`, `jsonable`, `truncate`, `changed_field_summary`, `record_club_history`, `field_label`
- **`html_sanitize.py`** (126 lines)
  Sanitizing the rich text people paste into Summernote.
  `sanitize_summernote_html`, `remove_html_color_tags`
- **`lifecycle.py`** (547 lines)
  Phase 9: measuring buyers, sellers and people with no account (``docs/phase_9.md``).
- **`llm.py`** (355 lines)
  Provider abstraction for everything on this site that talks to a language model.
- **`mailchimp.py`** (654 lines)
  One-way Django -> Mailchimp sync for clubs.
- **`middleware.py`** (61 lines)
  Custom middleware for the auctions application.
  `MobileAppMiddleware`
- **`model_caching.py`** (79 lines)
  ``@cached_property`` on a model, and the invalidation that makes it safe.
  `InvalidatesRelatedCache`, `CachedPropertiesMixin`
- **`models.py`** (13303 lines)
  The database: 80 models, mostly in one file because 29 of them form a single dependency cycle
- **`moderation_admin.py`** (139 lines)
  The Django admin for the moderation queue: reports, copyright notices and strikes.
  `ContentReportAdmin`, `CopyrightNoticeAdmin`, `CopyrightStrikeAdmin`
- **`moderation_forms.py`** (158 lines)
  The two forms behind the report button and the copyright notice page.
  `ReportContentForm`, `CopyrightNoticeForm`
- **`moderation_models.py`** (201 lines)
  Reports about content, copyright notices, and the strikes that come out of them.
  `ContentReport`, `CopyrightNotice`, `CopyrightStrike`
- **`module_map.py`** (181 lines)
  The map of this repository: which module does what, generated from the modules themselves.
  `Module`, `iter_modules`, `render`, `rule_violations`, `main`
- **`notifications.py`** (313 lines)
  Email to mobile-push routing.
  `push_configured`, `user_prefers_push`, `user_has_app_push`, `notify_user`, `notify_running_total`, `send_fcm_message`, `send_fcm_data_message`
- **`palette_actions.py`** (13166 lines)
  The things the command palette's assist, and ``/mcp/``, are allowed to do.
- **`palette_assist.py`** (1146 lines)
  Natural-language orchestration for the command palette.
- **`palette_routes.py`** (1830 lines)
  Every named URL, as a :class:`Route` the palette assistant can reach or an :data:`EXCLUDED` entry
  `Route`, `excluded_reason`, `is_third_party`, `audit`, `catalog_for_prompt`, `match_routes`, `get_route`, `route_needs_an_auction`, `resolve_route`, `page_context_from_path`
- **`passkit_views.py`** (181 lines)
  Apple PassKit web service: the endpoints installed Wallet passes talk to.
  `PassKitRegistrationView`, `PassKitDeviceRegistrationsView`, `PassKitPassView`, `PassKitLogView`
- **`printer_drafts.py`** (220 lines)
  Turn a characterized :class:`~auctions.models.ObservedPrinter` into a draft printer profile.
  `DraftError`, `profile_matches_observation`, `pick_gatt_ids`, `draft_slug`, `draft_profile_from_observation`
- **`printer_programs.py`** (675 lines)
  Validation and seed data for :class:`ThermalPrinterProfile` command programs.
  `ProgramValidationError`, `validate_program`, `validate_match_patterns`, `validate_profile_programs`, `serialize_profile`
- **`printing.py`** (318 lines)
  Shared label-printing helpers.
  `text_width_pt`, `split_label_tags`, `wrapped_lines`, `plan_label`, `inches_per_unit`, `deterministic_warnings`, `label_prefs_warnings`, `warning_matrix`
- **`queryset_annotations.py`** (223 lines)
  Queryset builders that answer a question about many rows at once.
  `nearby_auctions`, `add_tos_info`, `add_tos_distance_info`
- **`recurrence.py`** (151 lines)
  Repeating club events.
  `clean_lines`, `to_text`, `from_text`, `current_or_next`, `with_exdate`, `describe`
- **`routing.py`** (9 lines)
- **`serializers.py`** (855 lines)
  DRF serializers for the club API.
- **`services.py`** (1151 lines)
  Operations that are the same whoever asks: web page, API, app or assistant.
- **`signals.py`** (965 lines)
  Signal handlers for the auctions app.
- **`site_setup.py`** (146 lines)
  `single_club_mode_enabled`, `single_club_name`, `site_paypal_configured`, `get_server_public_ip`, `get_single_club`, `ensure_single_club_membership_for_user`
- **`social_adapter.py`** (57 lines)
  Site-specific allauth socialaccount adapter.
  `FishAuctionsSocialAccountAdapter`
- **`source_code.py`** (438 lines)
  This site's own source code, read out of the public repository it is published from.
- **`speaker_topics.py`** (200 lines)
  The speaker directory's fixed topic vocabulary.
  `canonical_topic_name`, `topic_needs_review`, `ensure_speaker_topics`
- **`species_categories.py`** (421 lines)
  Turn a species' taxonomy into one of the site's :class:`~auctions.models.Category` rows.
  `normalize_category_name`, `CategoryResolver`, `hint_for`, `assign_categories`
- **`species_matching.py`** (1034 lines)
  Turn a typed lot name into a short list of species to pick from, or nothing.
- **`tables.py`** (1376 lines)
  The ``django_tables2`` tables behind every list on the site.
- **`tasks.py`** (1729 lines)
  Celery tasks for the auctions app. Many wrap the management command of the same name.
- **`template_a11y.py`** (93 lines)
  Two accessibility rules a template cannot break twice, checked against template source.
  `check_text`, `check_templates`, `main`
- **`template_lint.py`** (153 lines)
  Catches two silent template mistakes: no error, no warning, just a wrong page.
  `iter_template_files`, `check_text`, `check_modal_container`, `check_templates`, `main`
- **`test_account_deletion.py`** (708 lines)
  Tests for account deletion.
- **`test_account_nav.py`** (302 lines)
  The Account setup menu: `auctions/account_nav.py`, its sidebar, and /account/setup/.
  `SidebarReachTests`, `LandingTests`, `PaymentRowTests`, `NavbarTests`, `SettingsSplitTests`
- **`test_admin_paginator.py`** (79 lines)
  ``EstimatedCountPaginator``: what it counts exactly and what it guesses at.
  `EstimatedCountPaginatorTests`, `PageViewChangelistTests`
- **`test_admin_performance.py`** (342 lines)
  The admin's own query counts: no dropdown over an unbounded table, and no query per inline row.
  `NoDropdownOverAnUnboundedTableTests`, `AdminChangePageGrowthTests`
- **`test_app_links.py`** (154 lines)
  Part LINKS — the two files that make a site link open in the app.
  `AppLinkFilesTests`, `AppLinksUnconfiguredTests`
- **`test_apple_notifications.py`** (640 lines)
  Tests for Sign in with Apple server-to-server notifications.
  `AppleNotificationTestCase`, `AppleNotificationVerificationTests`, `AppleNotificationForgeryTests`, `AppleNotificationEventParsingTests`, `AppleConsentRevokedTests`, `AppleAccountDeleteTests`, `AppleEmailForwardingTests`, `AppleNotificationRetryTests`, `AppleNotificationErrorTypeTests`, `AppleNotificationChecklistTests`
- **`test_ar.py`** (1714 lines)
  Tests for AR lot scanning and location mapping.
- **`test_assistant_context.py`** (624 lines)
  What the assistant does when nobody is looking at a page.
- **`test_auction_links.py`** (590 lines)
  Auction join links, the lot list's behaviour, and the Cloudflare image pipeline.
  `AuctionJoinLinksUserTests`, `AuctionTOSEmailChangeGuardTests`, `RelinkAuctiontosUsersCommandTests`, `LotListUXTests`, `CloudflareImagesTests`
- **`test_auction_misc.py`** (624 lines)
  Tests for the smaller auction surfaces: pickup locations, stats, bulk pages, watching, images.
- **`test_auction_props.py`** (1262 lines)
  Tests for ``Auction`` computed properties.
  `AuctionPropertyTests`, `LotPropertyTests`, `LotInvoicePropertyTests`, `SellerInvoiceRemovedLotTests`, `BuyNowSellerCreditTests`
- **`test_auction_views.py`** (717 lines)
  Auction pages an admin edits: permissions, the edit form, custom fields and cloning.
  `AuctionViewPermissionTests`, `AuctionEditViewTests`, `AuctionCustomFieldsViewTests`, `AuctionCloneCustomFieldsTests`, `PayPalFormFieldVisibilityTests`, `LotListViewTests`, `MyLotsViewTests`, `AuctionUsersViewTests`
- **`test_auctiontos.py`** (639 lines)
  ``AuctionTOS``: the admin filter over it, feedback, and merging two participants.
  `LotAdminFilterTests`, `FeedbackTestCase`, `AuctionHistoryTestCase`, `MergeAuctionTOSTests`, `AuctionTOSMergeViewTests`
- **`test_bap_lots.py`** (1403 lines)
  The breeder award program: which lots are eligible, and the pages that award points.
- **`test_bidding.py`** (1152 lines)
  Tests for bid values, bidding permissions, and the bid dialog.
  `LotPricesTests`, `DecimalBidValidationTests`, `BiddingPermissionsHardeningTests`, `AuctionEditFormMinimumBidTests`, `IntegerMoneyColumnRepairTests`, `CreateLotFormWholeDollarValidationTests`, `LotRefundDialogTests`, `BidDialogTests`, `WholeDollarBidBoxTests`
- **`test_bulk_add_lots.py`** (1255 lines)
  The bulk add-lots table, its per-row save, and the CSV import view.
  `BulkAddLotsAutoTests`, `UpdateAuctionStatsCommandTestCase`, `LotsByUserViewTest`, `ImportLotsFromCSVViewTests`
- **`test_cache_hygiene.py`** (132 lines)
  Guards against tests that clear a cache shared with every other parallel worker.
  `find_test_modules`, `check_source`, `CachesAreNotSharedBetweenWorkersTests`, `CacheHygieneCheckerTests`
- **`test_camera_scanner.py`** (140 lines)
  Guards the iPhone code path through the camera barcode scanner.
  `CameraScannerSourceTests`, `ScannerTemplateTests`, `QuickCheckoutCameraStartsOffTests`
- **`test_celery_tasks.py`** (1116 lines)
  Tests that Celery tasks call their corresponding management commands.
- **`test_checkin.py`** (638 lines)
  Tests for Part 6 — proximity check-in & welcome (mobile ping/join/set-location).
- **`test_club_announcements.py`** (1132 lines)
  Tests for club announcements, the website-integration snippets, and the embeds.
- **`test_club_api_read.py`** (1446 lines)
  The club REST API's read side: members, BAP lots, auctions and lots.
  `ClubAPITests`, `ClubAPIKeyMemberPermissionTests`, `ClubBapLotAPITests`, `ClubAuctionReadAPITests`, `ParseBoolEnvTests`, `RequireSecureProdSecretsTests`, `ClubAuctionIntegrationTests`
- **`test_club_events.py`** (2982 lines)
  Tests for club events, Google Calendar sync, and Discord events.
- **`test_club_finder.py`** (216 lines)
  Tests for the public club finder: what it lists, and what it refuses to say about a club.
  `map_payload`, `ClubFinderTests`
- **`test_club_health.py`** (631 lines)
  Tests for the club lifecycle rollup and the outreach queue.
- **`test_club_import.py`** (200 lines)
  Phase 8: importing a curated club list, and never publishing anything by accident.
  `csv_text`, `HostTests`, `ReadCsvTests`, `FindExistingTests`, `IngestTests`
- **`test_club_ledger.py`** (1136 lines)
  The club ledger on a cash basis: what a paid invoice freezes, and how dues reverse.
  `ClubMoneyLedgerCashBasisTests`, `PaidInvoiceFreezeTests`, `InvoiceDedupeLedgerTests`, `ClubMembershipDuesReversalTests`, `MakeClubAdminAssignsAuctionsTests`, `BapTop10ChartTests`, `ClubTreasurerReportViewTests`, `ClubTreasurerOutstandingInvoiceTests`
- **`test_club_linking.py`** (421 lines)
  The gate before creating an auction, and the repair queue for the auctions created before it.
  `MissingContactInfoTests`, `AuctionCreationGateTests`, `ClubNameMatchingTests`, `SuggestClubsTests`, `UnlinkedAuctionsPageTests`, `MakeClubAdminButtonTests`, `SuggestionShapeTests`
- **`test_club_money.py`** (761 lines)
  Money into and out of a club: PayPal without OAuth, invoices, profit and seller splits.
  `NonOAuthPayPalTests`, `ClubMoneyInvoiceHistoryTests`, `ClubProfitTests`, `TotalToSellersPercentToClubTests`, `AuctionGrossTests`
- **`test_club_permissions.py`** (959 lines)
  Club permissions in the awkward cases: wildcards, dialogs, Discord admin, view-only.
  `ClubPermissionWildcardTests`, `ClubPermissionsDialogTests`, `ClubMemberDiscordAdminViewTests`, `ClubMemberManagementViewTests`, `ClubViewOnlyAccessTests`, `ClubMembershipInvoiceTests`, `ClubMembershipSettingsFormFieldsTests`, `PaymentSellerClubLinkTests`
- **`test_club_settings.py`** (689 lines)
  A club's own settings pages: BAP, general settings and email routing.
  `ClubBapSettingsViewTests`, `ClubSettingsViewTests`, `ClubEmailRoutingTests`, `RoutedSenderDisplayNameTests`, `SesSendsTheMessagesOwnFromAddressTests`, `InboundEmailRoutingAPITests`, `AuctionSlugSanitizationTests`, `AuctionEmailSenderTests`, `ClubEmailSettingsFormTests`
- **`test_club_users.py`** (1396 lines)
  Managing people through a club rather than through an auction, and the bid API.
  `ManageUsersThroughClubTests`, `PlaceBidApiTests`
- **`test_clubs.py`** (1350 lines)
  Clubs: the model, the pages, and who is allowed to do what inside one.
  `ClubModelTests`, `ClubViewTests`, `ClubPermissionTests`, `ClubMemberUpdateTests`
- **`test_csv_import.py`** (1077 lines)
  Importing lots and users from a CSV or a club's Google Drive sheet.
  `AuctionHistoryTests`, `CSVImportTests`, `CSVImportBiddingPermissionTests`, `EnableBiddingForAllUsersTests`, `CSVImportPreviewTests`, `GoogleDriveImportTests`, `WeeklyPromoEmailTrackingTestCase`
- **`test_data_leak_penetration.py`** (259 lines)
  Penetration tests: no data leaks from public endpoints, as an unauthenticated user or a non-admin.
  `DataLeakPenetrationTests`
- **`test_dmca.py`** (468 lines)
  What the DMCA safe harbour needs to be true, checked.
  `AgentConfigurationTests`, `DmcaPageTests`, `NoticeIntakeTests`, `NoticeRoutingTests`, `ReportContentTests`, `StrikeTests`, `TakedownRemovesTheMaterialTests`, `MobileConfigTests`, `ImageSourceLabelTests`, `AccountDeletionTests`
- **`test_donations.py`** (1661 lines)
  Tests for donation tracking: routing, the inbound webhook, the LLM seams, and the UI gates.
- **`test_endauctions.py`** (902 lines)
  Tests for the ``endauctions`` command and the websocket consumers.
  `LotEndauctionsMethodsTests`, `WebsocketClientDisconnectTests`, `WebSocketConsumerTests`, `HasEverGrantedPermissionTests`
- **`test_form_friction.py`** (535 lines)
  Tests for the friction instrument: which form, which field, how many attempts, did they finish.
- **`test_helpers.py`** (1368 lines)
  Tests for helper functions, model utilities, template tags and context processors.
  `HelperFunctionsTestCase`, `ModelUtilityFunctionsTestCase`, `FormsUtilityTestCase`, `TemplateTagsTestCase`, `ContextProcessorsTestCase`, `FooterIconTests`, `SiteWebmanifestTests`, `GoogleLoginTemplateVisibilityTests`, `AdminSetupChecklistViewTests`
- **`test_invoice_models.py`** (222 lines)
  Invoice models: what an invoice contains, when it is created, and when it notifies.
  `InvoiceModelTests`, `InvoiceCreateViewTests`, `InvoiceNotificationDueTests`
- **`test_label_layout.py`** (331 lines)
  Every lot label preset, rendered with worst-case lots and held to the layout rules.
  `PlacedText`, `laid_out_text`, `squeezed`, `LabelLayoutTests`, `LabelUnitTests`, `LabelMeasurementTests`
- **`test_lifecycle.py`** (435 lines)
  Tests for phase 9: milestones, lapsing definition, session replay and cohorts.
  `ClubHistoryFixture`, `LapsingTests`, `CohortTests`, `SignInStitchTests`, `SessionTimelineTests`, `MedianMemberTests`, `UnreachedShareTests`, `MilestoneReachTests`, `LifecyclePageTests`
- **`test_login_required_dispatch.py`** (60 lines)
  Signed-out visitors are turned away, not handed a 500, by views that override ``dispatch``.
  `AnonymousDispatchTests`
- **`test_lot_create.py`** (542 lines)
  Creating a lot, and the invoice lists a seller and buyer see afterwards.
  `LotCreateViewTests`, `InvoiceViewTests`, `MyInvoicesListTests`
- **`test_lot_images.py`** (753 lines)
  Lot images: uploading, ordering, rotating and deleting them; plus signup forms.
  `LotImageManagementTests`, `ImageSourceOnThePageTests`, `ChangeUsernameFormTest`, `CustomSignupFormTest`, `AdminUserSignupsJSONTests`
- **`test_lot_models.py`** (930 lines)
  Lot and auction model behaviour, and the chat subscriptions hanging off a lot.
  `ViewLotTest`, `AuctionModelTests`, `LotModelTests`, `LotModelConcurrencyTests`, `ChatSubscriptionTests`
- **`test_lot_page_views.py`** (365 lines)
  The two page-view history modals: one lot's, and every lot on the selling dashboard.
  `PageViewHistoryHelperTests`, `LotPageViewHistoryViewTests`, `SellingDashboardPageViewHistoryTests`
- **`test_lot_views.py`** (1036 lines)
  The lot pages an auction is actually run from: labels, push, set-winner and the queue.
  `LotLabelViewTestCase`, `UpdateLotPushNotificationsViewTestCase`, `LotPushTestNotificationViewTestCase`, `ViewLotSimpleTestCase`, `DynamicSetLotWinnerViewTestCase`, `LotQueueViewTestCase`, `AlternativeSplitLabelTests`
- **`test_marketing.py`** (848 lines)
  Mailchimp and Brevo: syncing members, webhooks, self-service and what gets redacted.
- **`test_mcp.py`** (1339 lines)
  Tests for the MCP tool catalogue.
- **`test_mcp_permissions.py`** (629 lines)
  Every tool on ``/mcp/``, run against somebody else's club and auction.
  `secrets`, `CrossTenantTestCase`, `NobodyElsesDataTests`, `NobodyElsesRowsTests`, `NothingCrashesInsteadOfRefusingTests`, `PrintLabelsByPrimaryKeyTests`, `AuctionSetupBelongsToTheAuctionTests`, `ClubSetupBelongsToTheClubTests`
- **`test_mcp_resources.py`** (308 lines)
  The addressable reads and the recipes: ``resources/templates/list``, ``prompts/*``, completions.
  `ResourceCatalogueTests`, `ResourceEndpointTests`, `PromptTests`, `PromptEndpointTests`
- **`test_mcp_widgets.py`** (197 lines)
  Tests for the MCP-app widgets: the ``ui://`` resources a host renders instead of the JSON.
  `BundleTests`, `CatalogueTests`, `DocumentTests`, `ResourceEndpointTests`
- **`test_membership_flow.py`** (1286 lines)
  Tests for club membership money: invoices, discounts, renewals and confirmation emails.
  `InvoiceStatusButtonTests`, `ClubMembershipRenewalFlowTests`, `PayPalSubscriptionWebhookTests`, `ClubMemberDiscountTests`, `ClubMoneyRenewalConsistencyTests`, `ClubMembershipEmailTaskTests`, `ClubBarcodeViewTests`, `QuickCheckoutHTMXTests`
- **`test_mobile_features.py`** (2821 lines)
  Tests for the mobile-app web-side features.
- **`test_mobile_last_used.py`** (156 lines)
  Tests for GET /api/mobile/auctions/last-used/ — the command palette's AR-gating lookup.
  `MobileLastUsedAuctionTests`
- **`test_mobile_menu.py`** (323 lines)
  The app's navigation drawer: /api/mobile/config/ -> "menu".
  `MenuPayloadTests`, `RowSanitizerTests`, `NavbarDriftTests`, `ConfigEndpointTests`
- **`test_mobile_offline.py`** (599 lines)
  Tests for the mobile offline-mode backend (in-person sale).
  `MobileOfflineSnapshotTests`, `MobileOfflineSyncTests`
- **`test_mobile_payments.py`** (669 lines)
  Tap to Pay from inside the app: confirming a payment, and which Square seller it routes to.
  `MobilePaymentConfirmTests`, `MobilePaymentEndpointTests`, `SquareSellerRoutingTests`, `SquareTokenHandoutAuditTests`
- **`test_mobile_social_auth.py`** (869 lines)
  Tests for native social sign-in (Apple, Google, Facebook).
- **`test_models_misc.py`** (1169 lines)
  Model methods, signal behaviour, and the management commands that email people.
  `ModelMethodsTestCase`, `SignalLogicTestCase`, `DuplicateAuctionTOSTests`, `AuctionNoShowURLEncodingTest`, `WeeklyPromoManagementCommandTests`, `AuctionTOSNotificationsCommandTests`
- **`test_module_map.py`** (151 lines)
  Guards the module map against drift and verifies module docstring rules are enforced.
  `ModuleMapIsCurrentTests`, `ModuleRulesTests`, `RuleCheckerTests`, `SummaryTests`, `ViewsPackageStaysAcyclicTests`
- **`test_page_view_beacon.py`** (158 lines)
  One view per page, recorded on every page, with no timer in front of it.
  `BeaconSourceTests`, `WhatCountsAsViewingAnAuctionTests`, `OneViewPerPageTests`, `RowsFromTheBeaconTests`
- **`test_page_view_history_is_kept.py`** (47 lines)
  Repeat views of a page are history, not duplicates.
  `RepeatViewsAreKeptTests`
- **`test_page_view_url.py`** (116 lines)
  The shape of ``PageView.url``: a site-relative path, on the way in and on the rows already there.
  `PageViewPathTests`, `MigrationHostListTests`, `PageViewCreateStoresAPathTests`
- **`test_palette_account.py`** (870 lines)
  The rest of the account, and the auction and club setup pages behind it.
- **`test_palette_assist.py`** (3769 lines)
  Tests for the command palette's natural-language assist.
- **`test_palette_core.py`** (1210 lines)
  The command palette itself, and the mobile surfaces that call into it.
  `CommandPaletteTests`, `MobileCommandPaletteTests`, `MobileMyClubsTests`, `MobileLabelTests`, `MobileConfigTests`, `FirebaseClientConfigParsingTests`, `SingleLotLabelPngTests`, `MobileEmailLoginTests`, `MobileWebSessionTests`
- **`test_palette_mic.py`** (101 lines)
  Guards for the command palette's microphone, which no other test can reach.
  `mic_branches`, `PaletteMicSourceTests`
- **`test_palette_routes.py`** (165 lines)
  Tests for the palette's page catalog.
  `RouteAuditTests`, `RouteMatchingTests`, `PageContextTests`
- **`test_palette_skills.py`** (3096 lines)
  Tests for what the command palette assistant can *do*.
- **`test_paypal.py`** (923 lines)
  PayPal: the webhooks, their event handlers, refund idempotency and the CSV export.
  `PayPalWebhookViewTests`, `PayPalWebhookEventHandlerTests`, `RefundWebhookIdempotencyTests`, `SquarePaymentUpdatedRefundResurrectionTests`, `PayPalCSVExportTests`
- **`test_query_counts.py`** (559 lines)
  Query-count guards for the N+1s that were fixed.
  `QueryGrowthMixin`, `AuctionUsersTableQueryCountTests`, `AuctionLotAdminTableQueryCountTests`, `LotDetailQueryCountTests`, `InvoiceQueryCountTests`, `SellerAndFeedbackQueryCountTests`, `LongLivedInstanceTests`, `CachedPropertyWiringTests`, `LotListQueryCountTests`, `LotCachedPropertyTests`
- **`test_remote_print.py`** (539 lines)
  Printing from a computer to the phone's Bluetooth label printer.
- **`test_security.py`** (309 lines)
  Tests that AuctionTOS and user data are protected: unauthenticated and non-admin users can't reach
  `AuctionTOSSecurityTestCase`
- **`test_site_config.py`** (782 lines)
  Site-wide configuration: currency, email fields, locations, demo data and defaults.
  `CurrencyCustomizationTests`, `AuctionEmailFieldsTest`, `UserLocationUpdateTests`, `LoadDemoDataTests`, `EnsureSiteDefaultsCommandTests`, `AdminReadonlyFieldsTests`
- **`test_source_code.py`** (324 lines)
  The source code reader: what it can serve, and -- much more to the point -- what it cannot.
  `FakeResponse`, `fake_get`, `SourceTestCase`, `RepositorySettingTests`, `TheArchiveIsTheAllowlistTests`, `ReadingTests`, `ContentSearchTests`, `ReadSourceToolTests`
- **`test_speakers.py`** (1366 lines)
  Tests for the speaker directory: the NEC WordPress import, NEC-only scoping, the list and map view,
- **`test_species.py`** (4938 lines)
  Tests for scientific names on lots: matching, the picker, labels, and genus BAP points.
- **`test_square.py`** (910 lines)
  Square: taking a payment, refunding one, the OAuth grant, and webhook signatures.
  `SquarePaymentTests`, `SquareRefundFormTests`, `SquarePaymentSuccessViewTests`, `SquareOAuthRevocationTests`, `SquareWebhookSignatureValidationTests`
- **`test_static_files.py`** (147 lines)
  `/static/`: content-hashed names, and the nginx rule that caches them for a year.
  `TemplatesNameRealFilesTests`, `HashedNamesReachTheYearLongCacheTests`, `MissingManifestEntriesDoNotRaiseTests`, `DebugSkipsHashingTests`
- **`test_stats.py`** (1183 lines)
  The numbers on an auction's stats page, and the invoice wording that quotes them.
- **`test_support.py`** (40 lines)
  Support code shared by the test modules. Holds no tests of its own.
  `isolated_cache`
- **`test_support_page.py`** (264 lines)
  /support/, and a way to reach a human that works with no account.
  `SupportUrlWorksSignedOutTests`, `SupportPageIsTheHelpPageTests`, `OldContactUrlStillWorksTests`, `VideoEmbedFitsItsContainerTests`, `SupportFormDeliveryTests`, `SupportFormSignedInTests`
- **`test_tap_to_pay.py`** (1128 lines)
  Tests for the Tap to Pay on iPhone review-guide work (TTP-1..4).
- **`test_template_a11y.py`** (97 lines)
  Guards the two accessibility rules in auctions/template_a11y.py.
  `TemplatesAreAccessibleTests`, `CheckerBehaviourTests`, `HtmxAnnouncementTests`
- **`test_template_hygiene.py`** (141 lines)
  Guards against the template mistakes that produce a wrong page without an error.
  `TemplateTagsAreParseableTests`, `TemplateLintTests`, `OneModalContainerPerPageTests`
- **`test_usability_instruments.py`** (401 lines)
  Tests for the measurement half of the usability campaign: what an edit changed, and who has ever
  `JsonableTests`, `SecretFieldTests`, `ChangedFieldSummaryTests`, `AuctionHistoryChangedFieldsTests`, `ClubHistoryChangedFieldsTests`, `FieldAdoptionTests`, `AuctionEditFormLayoutTests`
- **`test_usability_report.py`** (307 lines)
  Tests for the usability dashboard's three panels, and for the URL classifier behind the first.
  `RouteNameTests`, `ReachTests`, `FrictionReportTests`, `BuyerFunnelTests`, `DashboardTests`
- **`test_user_features.py`** (532 lines)
  Tests for preferences that change what a user sees: distance units, exports, and trust.
  `DistanceUnitTests`, `PayPalInfoViewTests`, `UserExportTests`, `UserTrustSystemTests`, `WatchOrUnwatchViewTests`, `AdFetchTests`
- **`test_userdata.py`** (299 lines)
  ``UserData`` and ``AuctionTOS`` properties, and merging one user into another.
  `AuctionTOSPropertyTests`, `UserDataPropertyTests`, `UserDataMergeIntoTests`
- **`test_voice.py`** (721 lines)
  Voice-driven set winners.
  `VoiceV1RemovedTests`, `VoiceVocabularyTests`, `VoiceVocabularyClubManagedTests`, `VoiceConfigBlockTests`, `VoicePageTests`, `VoiceCommandLogTests`, `VoiceUnmatchedLogTests`, `VoiceLogAdminTests`, `VoiceSettingsPanelTests`, `PriceAnchorCanonicalWordTests`
- **`test_volunteers.py`** (268 lines)
  Tests for Part 7 — recruit volunteers (web feature).
  `VolunteerBase`, `VolunteerPageGatingTests`, `VolunteerHelperCountTests`, `VolunteerCreateTests`, `VolunteerSignupTests`, `VolunteerPageWarningTests`
- **`test_wallet_passes.py`** (1113 lines)
  Membership cards: Google Wallet, Apple Wallet, PassKit and the numbers on them.
  `DiscordJoinModalNameTests`, `DiscordJoinButtonTests`, `ClubMemberNameModelTests`, `ClubMemberIngestNameTests`, `GoogleWalletClassCreateTests`, `MembershipNumberUniquenessTests`, `AppleWalletPassTests`, `PassKitWebServiceTests`, `MembershipNumberModeTests`, `ClubIconWalletTests`
- **`test_wallet_status.py`** (1318 lines)
  Wallet status text, error-page logging, and the label-printing surfaces in the app.
- **`tests.py`** (324 lines)
  Shared test fixture and helpers every other test module builds on: StandardTestCase, WritableMediaRoot, patch_views.
  `patch_views`, `WritableMediaRoot`, `give_contact_info`, `CsvImportTestMixin`, `StandardTestCase`, `SuiteStaysFastTests`, `EveryTestStartsInTheSiteTimezoneTests`
- **`tests_selenium.py`** (1139 lines)
  Selenium browser tests for client-side JavaScript, HTMx and websockets.
- **`urls.py`** (1287 lines)
  Every URL on the site, and the one place a new one has to be declared.
- **`usability_report.py`** (276 lines)
  The usability measurements for the dashboard.
  `route_name`, `reach_by_route`, `friction_by_form`, `abandoned_durations`, `worst_fields`, `buyer_funnel`, `funnel_referrers`
- **`validators.py`** (19 lines)
  `validate_username_no_at_symbol`
- **`voice.py`** (297 lines)
  Voice-driven set winners: the grammar the mobile app listens with.
  `default_anchors`, `default_number_words`, `default_homophones`, `default_weights`, `default_thresholds`, `log_command`, `log_unmatched`, `serialize_grammar`, `page_config`

## `auctions/management/`


## `auctions/management/commands/`

- **`assign_auction_to_club.py`** (99 lines)
  `Command`
- **`auction_emails.py`** (344 lines)
  The nightly email about auctions worth knowing about, and the Discord post beside it.
  `Command`
- **`auctiontos_notifications.py`** (220 lines)
  `send_tos_notification`, `Command`
- **`backfill_bap_reasons.py`** (120 lines)
  `Command`
- **`backfill_club_members_into_auctions.py`** (95 lines)
  `Command`
- **`backfill_lot_species.py`** (497 lines)
  Attach a species to lots that predate the species list.
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
- **`endauctions.py`** (139 lines)
  `declare_winners_on_lots`, `deactivate_pretty_much_over_lots`, `Command`
- **`ensure_site_defaults.py`** (71 lines)
  `Command`
- **`ensure_speaker_topics.py`** (20 lines)
  Create the speaker directory's fixed topic vocabulary.
  `Command`
- **`find_square_reconnects.py`** (46 lines)
  `Command`
- **`geocode_speakers.py`** (195 lines)
  Backfill speaker locations that the NEC WordPress export didn't carry.
  `Command`
- **`import_clubs.py`** (63 lines)
  Import a curated CSV of aquarium clubs.  See auctions/club_import.py for why this is a CSV.
  `Command`
- **`import_fishbase.py`** (498 lines)
  Load the species picklist from a pinned FishBase snapshot, plus the curated aquarium list.
  `Command`
- **`import_nec_speakers.py`** (402 lines)
  Import the Northeast Council's speaker database from a WordPress WXR export.
  `clean_text`, `Command`
- **`load_demo_data.py`** (86 lines)
  Management command to load demo data for development environments.
  `Command`
- **`migrate_to_cloudflare_images.py`** (122 lines)
  Move locally stored images to Cloudflare Images.
  `Command`
- **`mine_palette_shortcuts.py`** (174 lines)
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
- **`sendnotifications.py`** (64 lines)
  `Command`
- **`set_user_location.py`** (157 lines)
  `Command`
- **`setup_celery_beat.py`** (127 lines)
  Create the PeriodicTask rows django-celery-beat reads, from the beat_schedule in
  `Command`
- **`split_speaker_talks.py`** (174 lines)
  Recover the individual talk titles from an imported speaker's run-on "Programs:" list.
  `normalize`, `split_is_faithful`, `Command`
- **`sync_google_wallet_classes.py`** (104 lines)
  `Command`
- **`tap_to_pay_launch_announcement.py`** (170 lines)
  The Tap to Pay on iPhone launch announcement (Apple marketing requirements 6.1 and 6.3).
  `Command`
- **`update_ar_positions.py`** (49 lines)
  Re-solve AR lot positions for auctions with fresh sightings, and prune the observation buffer.
  `Command`
- **`update_auction_stats.py`** (30 lines)
  `Command`
- **`update_breederboard.py`** (90 lines)
  `Command`
- **`update_user_interest.py`** (32 lines)
  `updateInterest`, `Command`
- **`webpush_notifications_deduplicate.py`** (19 lines)
  `Command`
- **`weekly_promo.py`** (253 lines)
  `Command`

## `auctions/mcp/`

The site's Model Context Protocol server, and the tool catalogue behind it.

- **`auth.py`** (260 lines)
  Who is calling ``/mcp/``, and what they may do.
- **`cimd.py`** (49 lines)
  Client ID Metadata Document handling for the clients that actually turn up.
  `supported_grant_types`, `narrow_grant_types`, `ClientMetadataFetcher`
- **`icons.py`** (100 lines)
  Icons for the tools, the prompts, the resources and the server itself.
  `domain`, `absolute`, `icons`, `for_action`, `for_prompt`, `for_uri`, `server`
- **`prompts.py`** (238 lines)
  Prompts: multi-step recipes offered to the *person* to pick off a menu, not to the model.
  `Argument`, `Prompt`, `descriptors`, `prompt_list`, `render`, `complete`, `completes`
- **`protocol.py`** (252 lines)
  JSON-RPC 2.0 and the MCP methods, with no HTTP in it.
  `Caller`, `error`, `is_notification`, `negotiate`, `handle`
- **`resources.py`** (352 lines)
  Addressable reads: the read-only tools' answers, reachable by URI.
  `Template`, `template_descriptors`, `fixed_descriptors`, `match`, `read`, `links_for`
- **`tools.py`** (365 lines)
  The action registry, as MCP tools.
- **`transport.py`** (139 lines)
  The HTTP end of the MCP server: one view, at ``/mcp/``. Nothing here knows what a tool is.
  `MCPEndpointView`
- **`widgets.py`** (153 lines)
  Interactive views this server publishes as MCP-app widgets.
  `resource_descriptors`, `read_resource`, `tool_meta`

## `auctions/mobile/`

- **`authentication.py`** (43 lines)
  Authentication classes for mobile endpoints.
  `OptionalJWTAuthentication`
- **`menu.py`** (185 lines)
  The app's navigation drawer, built here and served in /api/mobile/config/.
  `menu_for`
- **`permissions.py`** (17 lines)
  `IsMobileAuthenticated`
- **`renderers.py`** (49 lines)
  DRF renderers for mobile endpoints that return raw bytes.
  `BinaryRenderer`, `PdfRenderer`, `PngRenderer`
- **`serializers.py`** (533 lines)
  Request and response shapes for the mobile app's API under ``/api/mobile/``.
- **`urls.py`** (138 lines)
- **`views.py`** (1557 lines)
  Mobile API views: everything under /api/mobile/.

## `auctions/mobile/services/`

- **`ar.py`** (418 lines)
  AR lot scanning: overlay and card metadata, observation ingestion, and position payloads.
  `ar_dirty_key`, `mark_auction_dirty`, `drain_dirty_auction_pks`, `locatable_auction_pks`, `build_lot_metadata`, `ingest_observations`, `record_ar_events`, `positions_payload`, `clear_positions`
- **`auth.py`** (59 lines)
  `MobileAuthService`
- **`checkin.py`** (297 lines)
  Proximity check-in and welcome.
  `evaluate_ping`, `join_auction`, `set_auction_location`
- **`devices.py`** (85 lines)
  `DeviceService`
- **`label_pdf.py`** (72 lines)
  Single-lot label PDF for the mobile ``fishauctions://print/<pk>`` deep link.
  `build_label_view`, `render_view_pdf`, `render_single_lot_pdf`
- **`label_raster.py`** (140 lines)
  Rasterize the label PDF, so the Bluetooth PNG *is* the PDF.
  `rasterize_pdf`, `render_lot_label_png`, `render_lot_labels_png`
- **`label_renderers.py`** (177 lines)
  Fallback label rendering for the mobile app.
  `LabelRenderer`, `PngLabelRenderer`, `get_renderer`, `supported_formats`
- **`labels.py`** (109 lines)
  `LabelService`
- **`offline.py`** (534 lines)
  Offline mode for the app's in-person sale screens.
  `get_last_admin_auction`, `build_snapshot`, `apply_ops`
- **`payments.py`** (571 lines)
  Taking a card payment in the room, through the app's Tap to Pay.
  `PaymentVerificationError`, `PaymentAlreadyChargedError`, `TapToPayAttemptOpen`, `SquareReconnectRequired`, `PaymentService`
- **`printers.py`** (138 lines)
  Recording which Bluetooth printers users actually pair, and how they were identified.
  `record_observation`
- **`remote_print.py`** (147 lines)
  Printing from a computer to the phone's Bluetooth label printer.
  `heartbeat`, `wants_print_from_computer`, `create_job`, `dispatch`, `start`, `job_state`
- **`social_auth.py`** (355 lines)
  Native social sign-in for the mobile app: verify a provider credential, then let allauth decide.
  `SocialAuthError`, `build_sociallogin`, `PendingSocialLogin`, `resolve_completed_user`
- **`voice.py`** (97 lines)
  The vocabulary the app matches spoken words against, for one auction.
  `lot_numbers`, `bidder_numbers`, `build_vocabulary`
- **`web_session.py`** (81 lines)
  `mark_session_opened_by_app`, `session_opened_by_app`, `WebSessionService`

## `auctions/templatetags/`

- **`bap_filters.py`** (9 lines)
  `get_attr`
- **`club_nav_tags.py`** (96 lines)
  `club_sidebar`
- **`currency_filters.py`** (38 lines)
  `currency_symbol`, `format_price`
- **`distance_filters.py`** (56 lines)
  `convert_distance`, `distance_display`
- **`membership_tags.py`** (114 lines)
  `membership_barcode`, `google_wallet_save_url`
- **`species_tags.py`** (11 lines)
  `fishbase_citation`

## `auctions/views/`

Every view on the site, split by the part of it the view belongs to.

- **`account.py`** (556 lines)
  The reader's own account: profile, username, preferences, notifications, deletion.
- **`admin_checklist.py`** (1059 lines)
  The admin setup checklist: the one page that says what a new site still needs.
  `AdminSetupChecklistView`
- **`ajax.py`** (756 lines)
  The small endpoints pages call: POST targets, HTMx fragments and moderation actions.
- **`auction_admin.py`** (1218 lines)
  Setting an auction up and running the room: pickup locations, users, check-in.
- **`auction_extras.py`** (658 lines)
  The rest of an auction's admin surface: label config, bulk printing, no-shows, chat.
- **`auction_pages.py`** (1010 lines)
  The auction as a thing you join: the TOS, creating one, and the auction's own page.
  `AuctionTOSDelete`, `AuctionTOSAdmin`, `AuctionConfirmView`, `AuctionCreateView`, `AuctionInfo`
- **`auction_stats.py`** (1181 lines)
  The JSON behind the charts on one auction's stats page, one view per chart, all behind
- **`bap.py`** (622 lines)
  The breeder award program: settings, overrides, awards and the lots behind them.
- **`base.py`** (1016 lines)
  Shared view machinery: the mixins that decide who may see a page.
- **`browse.py`** (813 lines)
  The lot lists people browse, and what they do to a lot without opening it.
- **`bulk_actions.py`** (477 lines)
  The bulk buttons on the auction admin pages: mark paid, set won, enable bidding.
  `GetClubs`, `BulkSetLotsWon`, `InvoiceBulkUpdateStatus`, `MarkInvoicesReady`, `MarkInvoicesPaid`, `EnableBiddingForAllUsers`, `LotRefundDialog`
- **`bulk_add.py`** (887 lines)
  Bulk-adding people: bulk add users, and a club's shared spreadsheet.
  `CSVContactImportMixin`, `BulkAddUsers`, `ImportFromGoogleDrive`
- **`bulk_add_lots.py`** (953 lines)
  Getting lots in at once: the bulk table, the quick-add page, and the CSV importer.
  `BulkAddLots`, `BulkAddLotsAuto`, `SaveLotAjax`, `ImportLotsFromCSV`
- **`club_admin.py`** (1044 lines)
  Setting a club up: its details, membership settings, payment accounts, email.
- **`club_api.py`** (1134 lines)
  The club REST API: ``/api/v1/clubs/<slug>/…``.
- **`club_api_keys.py`** (381 lines)
  Club API keys, and the page that documents the API they open.
  `ClubAPIKeyListView`, `ClubAPIKeyCreateView`, `club_api_documentation_context`, `ClubAPIKeyDetailView`, `ClubAPIKeyRevokeView`, `ClubAPIKeyFieldMapCreateView`, `ClubAPIKeyFieldMapDeleteView`, `ClubMemberMapView`, `SelfServeContactLinkView`
- **`club_finder.py`** (196 lines)
  The public club finder: a map of clubs, and the same clubs as a filtered list.
  `ClubFinderView`
- **`club_integrations.py`** (1241 lines)
  The outside accounts a club connects: Mailchimp, Brevo, Google Calendar, Square links.
- **`club_members.py`** (1068 lines)
  The club's list of people: joining, renewing, permissions, cards.
- **`club_pages.py`** (577 lines)
  A club's public page, and the two links that identify a member on it.
  `ClubDetailView`, `ClubMemberByUUIDView`, `ClubMemberByNumberView`, `ClubAdminView`
- **`club_reports.py`** (809 lines)
  What a club's officers read: history, stats, the treasurer's report, money in and out.
  `ClubHistoryView`, `ClubStatsView`, `ClubTreasurerReportView`, `ClubTreasurerReportExportView`, `ClubMoneyCreateView`, `ClubMoneyBalanceView`, `ClubMemberCSVImportView`, `ClubMemberCSVExportView`
- **`discord.py`** (940 lines)
  Discord: verifying signatures, answering interactions, and syncing roles.
  `InboundEmailRoutingView`, `verify_discord_signature`, `assign_discord_role`, `DiscordInteractionsView`, `LotBapPointsView`, `ClubDiscordConfigView`, `ClubDiscordFetchRolesView`, `ClubDiscordEditRoleView`, `ClubDiscordSetDefaultRoleView`, `ClubDiscordSendJoinMessageView`
- **`embeds.py`** (624 lines)
  The snippets a club puts on its own website, and the pages behind them.
- **`exports.py`** (974 lines)
  Taking data back out: the CSV exports, the reports, and the mailing list.
- **`invoices.py`** (384 lines)
  Invoices as a person reads them: the list, one invoice, and the no-login link.
  `Invoices`, `InvoiceCreateView`, `InvoiceView`, `InvoiceNoLoginView`, `SquarePaymentSuccessView`
- **`lot_pages.py`** (1329 lines)
  One lot: its page, its photos, and creating or editing it.
- **`moderation.py`** (217 lines)
  The copyright policy page, the notice form, and the report button on a lot.
  `DmcaPolicyView`, `CopyrightNoticeCreate`, `ReportContentCreate`
- **`palette.py`** (462 lines)
  The command palette's views (ask, execute, cancel, report) and ``/ai/``, the API keys and OAuth
- **`payments.py`** (1054 lines)
  Connecting PayPal and Square accounts and taking payments through them.
- **`printing.py`** (533 lines)
  Labels: what gets drawn on them, and getting them to a printer.
  `LotLabelView`, `UnprintedLotLabelsView`, `SingleLotLabelView`, `RemotePrintJobMixin`, `RemotePrintJobStatusView`, `RemotePrintJobRetryView`, `RemotePrintJobCancelView`
- **`selling.py`** (966 lines)
  Auction night: setting winners, the lot queue, and volunteers.
- **`site_admin.py`** (477 lines)
  The superuser's dashboard: traffic, signups, referrers, the user map.
- **`site_pages.py`** (559 lines)
  Pages that belong to the site rather than to an auction or club: the FAQ, support, the promo site,
- **`speakers.py`** (482 lines)
  The speaker directory: who will come and talk to a club, and what about.
  `NECSpeakerAccessMixin`, `SpeakerListView`, `SpeakerPanelView`, `SpeakerDetailView`, `SpeakerCreateView`, `SpeakerUpdateView`, `SpeakerDeleteView`, `SpeakerTagView`, `SpeakerCommentView`, `SpeakerCommentDeleteView`
- **`species.py`** (492 lines)
  Adding species and common names, and the superuser's cleanup queue.
- **`usability.py`** (278 lines)
  The usability dashboards: measurements, the buyer funnel, and club outreach.
  `AdminUsability`, `AdminClubHealth`, `ClubMarkContacted`, `UnlinkedAuctions`, `LinkAuctionsToClub`, `AdminLifecycle`, `AdminSessionReplay`
- **`webhooks.py`** (921 lines)
  Webhooks from PayPal, Square and the email provider: unauthenticated POSTs verified by signature.
  `PayPalWebhookView`, `PayPalSubscriptionWebhookView`, `SquareWebhookView`, `QuickCheckout`, `QuickCheckoutHTMX`

## `fishauctions/`

This will make sure the app is always imported when

- **`_env.py`** (74 lines)
  Helpers for parsing environment variables in settings.
  `parse_bool_env`, `require_secure_prod_secrets`, `env_has_real_value`
- **`asgi.py`** (66 lines)
  `LogWebsocketExceptions`
- **`asgi_old.py`** (25 lines)
  ASGI config for fishauctions project.
- **`celery.py`** (216 lines)
  Celery configuration: the app, its beat schedule, and the self-scheduling tasks started at boot.
  `start_auction_stats_task`, `start_bap_recalculation_tasks`, `debug_task`
- **`custom_scheduler.py`** (53 lines)
  Celery Beat scheduler working around a django-celery-beat 2.8.1 bug.
  `FixedDatabaseScheduler`
- **`firebase_config.py`** (88 lines)
  Parse the public Firebase client-config files that ship with the mobile build.
  `load_android_config`, `load_ios_config`, `load_firebase_client_config`
- **`settings.py`** (1116 lines)
  Django settings for fishauctions. Reads .env; variables are documented in .env.example.
- **`static_storage.py`** (52 lines)
  Content-hashed names for `/static/`, tolerant of the two things that would break a deploy.
  `CacheBustedStaticFilesStorage`
- **`test_runner.py`** (85 lines)
  The test runner: the cheap password hasher, and the timezone reset between tests.
  `reset_timezone_between_tests`, `use_fast_hashers`, `FastParallelTestSuite`, `FastTestRunner`
- **`urls.py`** (83 lines)
- **`uvicorn_worker.py`** (15 lines)
  Custom gunicorn worker that runs uvicorn on the stdlib asyncio loop.
  `AsyncioUvicornWorker`
- **`wsgi.py`** (16 lines)
  WSGI config for fishauctions project.
