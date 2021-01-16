import decimal
from django.utils import timezone
import datetime
from django.contrib.auth.models import *
from django.db import models
from django.core.validators import *
from django.db.models import Count, Sum, Q
from django.db.models.expressions import RawSQL
from autoslug import AutoSlugField
from django.urls import reverse
from markdownfield.models import MarkdownField, RenderedMarkdownField
from markdownfield.validators import VALIDATOR_STANDARD
from easy_thumbnails.fields import ThumbnailerImageField
from location_field.models.plain import PlainLocationField
from django.db.models.signals import pre_save
from django.dispatch import receiver
import uuid
from random import randint
from django.conf import settings

def median_value(queryset, term):
    count = queryset.count()
    return queryset.values_list(term, flat=True).order_by(term)[int(round(count/2))]

def distance_to(latitude, longitude, unit='miles'):
    """
    GeoDjango has been fustrating with MySQL and Point objects.
    This function is a workaound done using raw SQL.

    Given a latitude and longitude, it will return raw SQL that can be used to annotate a queryset

    The model being annotated must have fields named 'latitude' and 'longitude' for this to work

    For example:

    qs = model.objects.all()\
            .annotate(distance=distance_to(latitude, longitude))\
            .order_by('distance')
    """
    if unit == "miles":
        correction = 0.6213712 # close enough
    else:
        correction = 1 # km
    # Great circle distance formula
    gcd_formula = "ROUND( 6371 * acos(least(greatest( \
        cos(radians(%s)) * cos(radians(latitude)) \
        * cos(radians(longitude) - radians(%s)) + \
        sin(radians(%s)) * sin(radians(latitude)) \
        , -1), 1)) * %s, 0)"
    distance_raw_sql = RawSQL(
        gcd_formula,
        (latitude, longitude, latitude, correction)
    )
    return distance_raw_sql


class BlogPost(models.Model):
	"""
	A simple markdown blog.  At the moment, I don't feel that adding a full CMS is necessary
	"""
	title = models.CharField(max_length=255)
	slug = AutoSlugField(populate_from='title', unique=True)
	body = MarkdownField(rendered_field='body_rendered', validator=VALIDATOR_STANDARD, blank=True, null=True)
	body_rendered = RenderedMarkdownField(blank=True, null=True)
	date_posted = models.DateTimeField(auto_now_add=True)
	extra_js = models.TextField(max_length=16000, null=True, blank=True)

	def __str__(self):
		return self.title

class Location(models.Model):
	"""
	Allows users to specify a state
	"""
	name = models.CharField(max_length=255)
	def __str__(self):
		return str(self.name)

class Club(models.Model):
	"""Clubs restrict who can enter or bid in an auction"""
	name = models.CharField(max_length=255)
	def __str__(self):
		return str(self.name)

class Category(models.Model):
	"""Picklist of species.  Used for product, lot, and interest"""
	name = models.CharField(max_length=255)
	def __str__(self):
		return str(self.name)
	class Meta:
		verbose_name_plural = "Categories"
		ordering = ['name']
		
class Product(models.Model):
	"""A species or item in the auction"""
	common_name = models.CharField(max_length=255)
	common_name.help_text = "The name usually used to describe this species"
	scientific_name = models.CharField(max_length=255, blank=True)
	scientific_name.help_text = "Latin name used to describe this species"
	breeder_points = models.BooleanField(default=True)
	category = models.ForeignKey(Category, null=True, on_delete=models.SET_NULL)
	def __str__(self):
		return f"{self.common_name} ({self.scientific_name})"
	class Meta:
		verbose_name_plural = "Products and species"

class Auction(models.Model):
	"""An auction is a collection of lots"""
	title = models.CharField(max_length=255)
	slug = AutoSlugField(populate_from='title', unique=True)
	sealed_bid = models.BooleanField(default=False)
	sealed_bid.help_text = "Users won't be able to see what the current bid is"
	lot_entry_fee = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
	lot_entry_fee.help_text = "The amount, in dollars, that the seller will be charged if a lot sells"
	unsold_lot_fee = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
	unsold_lot_fee.help_text = "The amount, in dollars, that the seller will be charged if their lot doesn't sell"
	winning_bid_percent_to_club = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
	winning_bid_percent_to_club.help_text = "To give 70% of the final bid to the seller, enter 30 here"
	date_posted = models.DateTimeField(auto_now_add=True)
	date_start = models.DateTimeField()
	lot_submission_end_date = models.DateTimeField(null=True, blank=True)
	date_end = models.DateTimeField()
	watch_warning_email_sent = models.BooleanField(default=False)
	invoiced = models.BooleanField(default=False)
	created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	location = models.CharField(max_length=300, null=True, blank=True)
	location.help_text = "State or region of this auction"
	notes = MarkdownField(rendered_field='notes_rendered', validator=VALIDATOR_STANDARD, blank=True, null=True, verbose_name="Rules")
	notes.help_text = "To add a link: [Link text](https://www.google.com)"
	notes_rendered = RenderedMarkdownField(blank=True, null=True)
	code_to_add_lots = models.CharField(max_length=255, blank=True, null=True)
	code_to_add_lots.help_text = "This is like a password: People in your club will enter this code to put their lots in this auction"
	lot_promotion_cost = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
	first_bid_payout = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
	first_bid_payout.help_text = "This is a feature to encourage bidding.  Give each bidder this amount, for free.  <a href='/blog/encouraging-participation/' target='_blank'>More information</a>"
	promote_this_auction = models.BooleanField(default=True)
	promote_this_auction.help_text = "Notify users and post on social media about this auction.  Uncheck if this is a test or private auction."
	is_chat_allowed = models.BooleanField(default=True)
	max_lots_per_user = models.PositiveIntegerField(null=True, blank=True)
	max_lots_per_user.help_text = "A user won't be able to add more than this many lots to this auction"
	email_first_sent = models.BooleanField(default=False)
	email_second_sent = models.BooleanField(default=False)
	email_third_sent = models.BooleanField(default=False)
	email_fourth_sent = models.BooleanField(default=False)
	email_fifth_sent = models.BooleanField(default=False)
	
	# fixme - everything below here can be removed now
	pickup_location = models.CharField(max_length=300, null=True, blank=True)
	pickup_location.help_text = "Description of pickup location"
	pickup_location_map = models.CharField(max_length=2000, null=True, blank=True)
	pickup_location_map.help_text = "Find the location on Google maps, click Menu>Share or Embed Map and paste the embed link here"
	pickup_time = models.DateTimeField(null=True, blank=True)
	alternate_pickup_location = models.CharField(null=True, blank=True, max_length=300)
	alternate_pickup_location.help_text = "Description of alternate pickup location"
	alternate_pickup_location_map = models.CharField(null=True, blank=True, max_length=2000)
	alternate_pickup_location_map.help_text = "Google Maps link to alternate pickup location"
	alternate_pickup_time = models.DateTimeField(blank=True, null=True)
	# fixme - everything above here can be removed now

	def __str__(self):
		if "auction" not in self.title.lower():
			return f"{self.title} auction"
		return self.title
	
	def get_absolute_url(self):
		return reverse('slug', kwargs={'slug': self.slug})

	@property
	def ending_soon(self):
		"""Used to send notifications"""
		warning_date = self.date_end - datetime.timedelta(hours=2)
		if timezone.now() > warning_date:
			return True
		else:
			return False
	
	@property
	def closed(self):
		"""For display on the main auctions list"""
		if timezone.now() > self.date_end:
			return True
		else:
			return False

	@property
	def started(self):
		"""For display on the main auctions list"""
		if timezone.now() > self.date_start:
			return True
		else:
			return False

	@property
	def club_profit_raw(self):
		"""Total amount made by the club in this auction.  This number does not take into account rounding in the invoices"""
		allLots = Lot.objects.filter(auction=self.pk)
		total = 0
		for lot in allLots:
			total += lot.club_cut
		return total

	@property
	def club_profit(self):
		"""Total amount made by the club in this auction, including rounding in the customer's favor in invoices"""
		try:
			invoices = Invoice.objects.filter(auction=self.pk)
			total = 0
			for invoice in invoices:
				total -= invoice.rounded_net
			return total
		except:
			return 0

	@property
	def gross(self):
		"""Total value of all lots sold"""
		try:
			gross = Lot.objects.filter(auction=self.pk).aggregate(Sum('winning_price'))['winning_price__sum']
			if gross is None:
				gross = 0
			return gross
		except:
			return 0

	@property
	def total_to_sellers(self):
		"""Total amount paid out to all sellers"""
		return self.gross - self.club_profit

	@property
	def percent_to_club(self):
		"""Percent of gross that went to the club"""
		if self.gross:
			return self.club_profit/self.gross * 100
		else:
			return 0

	@property
	def number_of_confirmed_tos(self):
		"""How many people selected a pickup location in this auction"""
		return AuctionTOS.objects.filter(auction=self.pk).count()

	@property
	def number_of_sellers(self):
		#users = User.objects.values('lot__user').annotate(Sum('lot')).filter(lot__auction=self.pk, lot__winner__isnull=False)
		users = User.objects.filter(lot__auction=self.pk, lot__winner__isnull=False).distinct()
		return len(users)

	# @property
	# def number_of_unsuccessful_sellers(self):
	#	"""This is the number of sellers who didn't sell ALL their lots"""
	# 	users = User.objects.values('lot__user').annotate(Sum('lot')).filter(lot__auction=self.pk, lot__winner__isnull=True)
	#   users = User.objects.filter(lot__auction=self.pk, lot__winner__isnull=True).distinct()
	# 	return len(users)

	@property
	def number_of_buyers(self):
		#users = User.objects.values('lot__winner').annotate(Sum('lot')).filter(lot__auction=self.pk)
		users = User.objects.filter(winner__auction=self.pk).distinct()
		return len(users)

	@property
	def users_signed_up(self):
		"""numbers users signed up druing this auction's open period"""
		#users = User.objects.values('').annotate(Sum('lot')).filter(lot__auction=self.pk)
		return False #len(users)
	# users who bought a single lot
	# users who viewed but didn't bid
	
	@property
	def median_lot_price(self):
		lots = Lot.objects.filter(auction=self.pk, winning_price__isnull=False)
		if lots:
			return median_value(lots,'winning_price')
		else:
			return 0
	
	@property
	def total_sold_lots(self):
		return len(Lot.objects.filter(auction=self.pk, winning_price__isnull=False))

	@property
	def total_unsold_lots(self):
		return len(Lot.objects.filter(auction=self.pk, winning_price__isnull=True))

	@property
	def total_lots(self):
		return len(Lot.objects.filter(auction=self.pk))

	@property
	def percent_unsold_lots(self):
		try:
			return self.total_unsold_lots / self.total_lots * 100
		except:
			return 100
	
	@property
	def can_submit_lots(self):
		if timezone.now() < self.date_start:
			return False
		if self.lot_submission_end_date:
			if self.lot_submission_end_date < timezone.now():
				return False
			else:
				return True
		if self.date_end > timezone.now():
			return False
		return True
	
	@property
	def bin_size(self):
		"""Used for auction stats graph - on the lot sell price chart, this is the the size of each bin"""
		try:
			return int(self.median_lot_price/5)
		except:
			return 2
	@property
	def number_of_participants(self):
		"""
		Number of users who bought or sold at least one lot
		"""
		buyers = User.objects.filter(winner__auction=self.pk).distinct()
		sellers = User.objects.filter(lot__auction=self.pk, lot__winner__isnull=False).exclude(id__in=buyers).distinct()
		return len(sellers) + len(buyers)

	@property
	def multi_location(self):
		"""
		True if there's more than one location at this auction
		"""
		locations = PickupLocation.objects.filter(auction=self.pk).count()
		if locations > 1:
			return True
		return False

	@property
	def no_location(self):
		"""
		True if there's no pickup location at all for this auction
		"""
		locations = PickupLocation.objects.filter(auction=self.pk)
		if not locations:
			return True
		return False
	
	@property
	def paypal_invoice_chunks(self):
		"""
		Needed to know how many chunks to split the inovice list to
		https://www.paypal.com/invoice/batch
		used by views.auctionInvoicesPaypalCSV
		"""
		invoices = Invoice.objects.filter(auction=self.pk)
		chunks = 1
		count = 0
		chunkSize = 150
		returnList = [1]
		for invoice in invoices:
			if not invoice.user_should_be_paid: # only include users that need to pay us
				count += 1
				if count > chunkSize:
					chunks += 1
					returnList.append(chunks)
					count = 0
		print(returnList)
		return returnList

class PickupLocation(models.Model):
	"""
	A pickup location associated with an auction
	A given auction can have multiple pickup locations
	"""
	name = models.CharField(max_length=50, default="")
	name.help_text = "Location name shown to users.  e.x. University Mall in VT"
	user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.CASCADE)
	auction.help_text = "If your auction isn't listed here, it may not exist or has already ended"
	description = models.CharField(max_length=300)
	description.help_text = "e.x. First floor of parking garage near Sears entrance"
	
	# fixme - this can be removed now
	#google_map_iframe = models.CharField(max_length=2000, blank=True, null=True)
	#google_map_iframe.help_text = "Find the location on Google maps, click Menu>Share or Embed Map and paste the embed link here.  You must embed an iframe, not a link."
		
	users_must_coordinate_pickup = models.BooleanField(default=False)
	users_must_coordinate_pickup.help_text = "The pickup time fields will not be used"

	pickup_location_contact_name = models.CharField(max_length=200, blank=True, null=True, verbose_name="Contact person's name")
	pickup_location_contact_name.help_text = "Name of the person coordinating this pickup location.  Contact info is only shown to logged in users."
	pickup_location_contact_phone = models.CharField(max_length=200, blank=True, null=True, verbose_name="Contact person's phone")
	pickup_location_contact_email = models.CharField(max_length=200, blank=True, null=True, verbose_name="Contact person's email")

	pickup_time = models.DateTimeField()
	second_pickup_time = models.DateTimeField(blank=True, null=True)
	second_pickup_time.help_text = "If you'll have a dropoff for sellers in the morning and then a pickup for buyers in the afternoon at this location, this should be the pickup time."
	latitude = models.FloatField(blank=True, null=True)
	longitude = models.FloatField(blank=True, null=True)
	address = models.CharField(max_length=500, blank=True, null=True)
	address.help_text = "Search Google maps with this address"
	location_coordinates = PlainLocationField(based_fields=['address'], blank=False, null=True, verbose_name="Map")
	
	def __str__(self):
		return self.name



class AuctionTOS(models.Model):
	user = models.ForeignKey(User, on_delete=models.CASCADE)
	auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
	pickup_location = models.ForeignKey(PickupLocation, on_delete=models.CASCADE) #null=True, on_delete=models.SET_NULL)
	def __str__(self):
		return f"{self.user} will meet at {self.pickup_location} for {self.auction}"
	class Meta: 
		verbose_name = "Auction pickup location"
		verbose_name_plural = "Auction pickup locations"

class Lot(models.Model):
	"""A lot is something to bid on"""
	PIC_CATEGORIES = (
		('ACTUAL', 'This picture is of the exact item'),
		('REPRESENTATIVE', "This is my picture, but it's not of this exact item.  e.x. This is the parents of these fry"),
		('RANDOM', 'This picture is from the internet'),
	)
	lot_number = models.AutoField(primary_key=True)
	lot_name = models.CharField(max_length=40, default="")
	slug = AutoSlugField(populate_from='lot_name', unique=False)
	lot_name.help_text = "Short description of this lot"
	image = ThumbnailerImageField(upload_to='images/', blank=True)
	image.help_text = "Optional.  Add a picture of the item here."
	image_source = models.CharField(
		max_length=20,
		choices=PIC_CATEGORIES,
		blank=True
	)
	image_source.help_text = "Where did you get this image?"
	i_bred_this_fish = models.BooleanField(default=False, verbose_name="I bred this fish/propagated this plant")
	i_bred_this_fish.help_text = "Check to get breeder points for this lot"
	description = MarkdownField(rendered_field='description_rendered', validator=VALIDATOR_STANDARD, blank=True, null=True)
	description.help_text = "To add a link: [Link text](https://www.google.com)"
	description_rendered = RenderedMarkdownField(blank=True, null=True)
	
	quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
	quantity.help_text = "How many of this item are in this lot?"
	reserve_price = models.PositiveIntegerField(default=2, validators=[MinValueValidator(1), MaxValueValidator(200)])
	reserve_price.help_text = "The minimum bid for this lot. Lot will not be sold unless someone bids at least this much"
	buy_now_price = models.PositiveIntegerField(default=None, validators=[MinValueValidator(1), MaxValueValidator(200)], blank=True, null=True)
	buy_now_price.help_text = "This lot will be sold instantly for this price if someone is willing to pay this much"
	species = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
	species_category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Category")
	species_category.help_text = "If you don't want to pick a species/product, pick the category this lot belongs to"
	date_posted = models.DateTimeField(auto_now_add=True, blank=True)
	user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	auction = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
	auction.help_text = "Only auctions that you have <span class='text-warning'>selected a pickup location for</span> will be shown here. This lot must be brought to that location"
	date_end = models.DateTimeField(auto_now_add=False, blank=True, null=True)
	winner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="winner")
	active = models.BooleanField(default=True)
	winning_price = models.PositiveIntegerField(null=True, blank=True)
	banned = models.BooleanField(default=False)
	banned.help_text = "This lot will be hidden from views, and users won't be able to bid on it.  Banned lots are not charged in invoices."
	ban_reason = models.CharField(max_length=100, blank=True, null=True)
	donation = models.BooleanField(default=False)
	donation.help_text = "All proceeds from this lot will go to the club"
	watch_warning_email_sent = models.BooleanField(default=False)
	seller_invoice = models.ForeignKey('Invoice', null=True, blank=True, on_delete=models.SET_NULL, related_name="seller_invoice")
	buyer_invoice = models.ForeignKey('Invoice', null=True, blank=True, on_delete=models.SET_NULL, related_name="buyer_invoice")
	transportable = models.BooleanField(default=True)
	promoted = models.BooleanField(default=False, verbose_name="Promote this lot")
	promoted.help_text = "This does nothing right now lol"
	promotion_weight = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(20)])
	feedback_rating = models.IntegerField(default = 0, validators=[MinValueValidator(-1), MaxValueValidator(1)])
	feedback_text = models.CharField(max_length=100, blank=True, null=True)
	winner_feedback_rating = models.IntegerField(default = 0, validators=[MinValueValidator(-1), MaxValueValidator(1)])
	winner_feedback_text = models.CharField(max_length=100, blank=True, null=True)
	date_of_last_user_edit = models.DateTimeField(auto_now_add=True, blank=True)
	is_chat_allowed = models.BooleanField(default=True)
	is_chat_allowed.help_text = "Uncheck to prevent chatting on this lot.  This will not remove any existing chat messages"

	# Location, populated from userdata.  This is needed to prevent users from changing their address after posting a lot
	latitude = models.FloatField(blank=True, null=True)
	longitude = models.FloatField(blank=True, null=True)
	address = models.CharField(max_length=500, blank=True, null=True)
	
	# Payment and shipping options, populated from last submitted lot
	# Only show these fields if auction is set to none
	payment_paypal = models.BooleanField(default=False, verbose_name="Paypal accepted")
	payment_cash = models.BooleanField(default=False, verbose_name="Cash accepted")
	payment_other = models.BooleanField(default=False, verbose_name="Other payment method accepted")
	payment_other_method = models.CharField(max_length=80, blank=True, null=True, verbose_name="Payment method")
	payment_other_address = models.CharField(max_length=200, blank=True, null=True, verbose_name="Payment address")
	payment_other_address.help_text = "The address or username you wish to get payment at"
	# shipping options
	local_pickup = models.BooleanField(default=False)
	local_pickup.help_text = "Check if you'll meet people in person to exchange this lot"
	#other = models.BooleanField(default=False) # fixme - this can be removed

	other_text = models.CharField(max_length=200, blank=True, null=True, verbose_name="Shipping notes") # fixme - require this if other location is selected
	other_text.help_text = "Shipping methods, temperature restrictions, etc."
	shipping_locations = models.ManyToManyField(Location, blank=True, verbose_name="I will ship to")
	shipping_locations.help_text = "Check all locations you're willing to ship to"

	def __str__(self):
		return "" + str(self.lot_number) + " - " + self.lot_name

	@property
	def winner_location(self):
		"""String of location of the winner for this lot"""
		try:
			return str(AuctionTOS.objects.get(user=self.winner, auction=self.auction).pickup_location)
		except:
			return ""
			
	@property
	def tos_needed(self):
		if not self.auction:
			return False
		try:
			AuctionTOS.objects.get(user=self.user, auction=self.auction)
			return False
		except:
			return f'/auctions/{self.auction.slug}'
		
	@property
	def location(self):
		"""String of location of the seller of this lot"""
		try:
			return str(AuctionTOS.objects.get(user=self.user, auction=self.auction).pickup_location)
		except:
			return ""
		# try:
		# 	return UserData.objects.get(user=self.user.pk).location
		# except:
		# 	return ""

	@property
	def user_as_str(self):
		"""String value of the seller of this lot"""
		return str(self.user)

	@property
	def payout(self):
		"""Used for invoicing"""
		payout = {
			"ended": False,
			"sold": False,
			"winning_price": 0,
			"to_seller": 0,
			"to_club": 0,
			"to_site": 0,
			}
		if self.auction:
			if not self.active:
				# bidding has officially closed
				payout['ended'] = True
				if self.banned:
					return payout
				auction = Auction.objects.get(id=self.auction.pk)
				if self.winner:
					# this lot sold
					payout['sold'] = True
					payout['winning_price'] = self.winning_price
					if self.donation:
						clubCut = self.winning_price
						sellerCut = 0
					else:
						clubCut = ( self.winning_price * auction.winning_bid_percent_to_club / 100 ) + auction.lot_entry_fee
						sellerCut = self.winning_price - clubCut
					payout['to_club'] = clubCut
					payout['to_seller'] = sellerCut
				else:
					# did not sell
					if not self.donation:
						payout['to_club'] = auction.unsold_lot_fee # bill the seller even if the item didn't sell
						payout['to_seller'] = 0 - auction.unsold_lot_fee
					else:
						payout['to_club'] = 0 # don't bill for donations
						payout['to_seller'] = 0
				if self.promoted:
					payout['to_club'] += auction.lot_promotion_cost					
		return payout

	@property
	def your_cut(self):
		return self.payout['to_seller']

	@property
	def club_cut(self):
		return self.payout['to_club']

	@property
	def number_of_watchers(self):
		return Watch.objects.filter(lot_number=self.lot_number).count()

	@property
	def calculated_end(self):
		if self.auction:
			auction = Auction.objects.get(pk=self.auction.pk)
			if auction.date_end > timezone.now():
				# if this lot was won by buy now
				if self.winner:
					return self.date_end
			return auction.date_end
		if self.date_end:
			return self.date_end
		return timezone.now()

	@property
	def can_be_edited(self):
		"""Check to see if this lot can be edited.
		This is needed to prevent people making lots a donation right before the auction ends"""
		if self.high_bidder:
			return False
		max_delete_date = self.date_posted + datetime.timedelta(hours=24)
		if timezone.now() > max_delete_date:
			return False
		else:
			return True
	
	@property
	def can_be_deleted(self):
		"""Check to see if this lot can be deleted.
		This is needed to prevent people deleting lots that don't sell right before the auction ends"""
		max_delete_date = self.date_posted + datetime.timedelta(hours=24)
		if timezone.now() > max_delete_date:
			return False
		else:
			return True

	@property
	def ended(self):
		"""Used by the view for display of whether or not the auction has ended
		See also the database field active, which is set by a system job"""
		if timezone.now() > self.calculated_end:
			return True
		else:
			return False

	@property
	def ending_soon(self):
		"""2 hours before - used to send notifications about watched lots"""
		warning_date = self.calculated_end - datetime.timedelta(hours=2)
		if timezone.now() > warning_date:
			return True
		else:
			return False

	@property
	def ending_very_soon(self):
		"""
		If a lot is about to end in less than a minute, notification will be pushed to the channel
		"""
		warning_date = self.calculated_end - datetime.timedelta(minutes=1)
		if timezone.now() > warning_date:
			return True
		else:
			return False

	@property
	def sealed_bid(self):
		if self.auction:
			if self.auction.sealed_bid:
				return True
		return False

	@property
	def max_bid(self):
		"""returns the highest bid amount for this lot - this number should not be visible to the public"""
		allBids = Bid.objects.filter(lot_number=self.lot_number, last_bid_time__lte=self.calculated_end, amount__gte=self.reserve_price).order_by('-amount', 'last_bid_time')[:2]
		try:
			# $1 more than the second highest bid
			bidPrice = allBids[0].amount
			return bidPrice
		except:
			#print("no bids for this item")
			return self.reserve_price

	@property
	def high_bid(self):
		"""returns the high bid amount for this lot"""
		if self.sealed_bid:
			try:
				bids = Bid.objects.filter(lot_number=self.lot_number, last_bid_time__lte=self.calculated_end, amount__gte=self.reserve_price).order_by('-amount', 'last_bid_time')
				return bids[0].amount
			except:
				return 0
		else:
			try:
				allBids = Bid.objects.filter(lot_number=self.lot_number, last_bid_time__lte=self.calculated_end, amount__gte=self.reserve_price).order_by('-amount', 'last_bid_time')[:2]
				# highest bid is the winner, but the second highest determines the price
				# $1 more than the second highest bid
				if allBids[0].amount == allBids[1].amount:
					return allBids[0].amount
				else:
					bidPrice = allBids[1].amount + 1
				return bidPrice
			except:
				#print("no bids for this item")
				return self.reserve_price

	@property
	def high_bidder(self):
		""" Name of the highest bidder """
		if self.banned:
			return False
		try:
			allBids = Bid.objects.filter(lot_number=self.lot_number, last_bid_time__lte=self.calculated_end, amount__gte=self.reserve_price).order_by('-amount', 'last_bid_time')[:2]
			return allBids[0].user
		except:
			return False

	@property
	def all_page_views(self):
		"""Return a set of all users who have viewed this lot, and how long they looked at it for"""
		return PageView.objects.filter(lot_number=self.lot_number)

	@property
	def anonymous_views(self):
		return len(PageView.objects.filter(lot_number=self.lot_number, user_id__isnull=True))

	@property
	def page_views(self):
		"""Total number of page views from all users"""
		pageViews = self.all_page_views
		return len(pageViews)

	@property
	def number_of_bids(self):
		"""How many users placed bids on this lot?"""
		bids = Bid.objects.filter(lot_number=self.lot_number, bid_time__lte=self.calculated_end, amount__gte=self.reserve_price)
		return len(bids)
	
	@property
	def view_to_bid_ratio(self):
		"""A low number here represents something interesting but not wanted.  A high number (closer to 1) represents more interest"""
		if self.page_views:
			return self.number_of_bids / self.page_views
		else:
			return 0

	@property
	def chat_allowed(self):
		if not self.is_chat_allowed:
			return False
		if self.auction:
			if not self.auction.is_chat_allowed:
				return False
		date_chat_end = self.calculated_end + datetime.timedelta(minutes=10)
		if timezone.now() > date_chat_end:
			return False
		return True

class Invoice(models.Model):
	"""
	An invoice is applied to an auction
	or a lot (if the lot is not associated with an auction)
	
	It's the total amount you owe and how much you owe to the club
	Invoices get rounded to the nearest dollar
	"""
	auction = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
	lot = models.ForeignKey(Lot, blank=True, null=True, on_delete=models.SET_NULL)
	lot.help_text = "not used"
	user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	seller = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="seller")
	date = models.DateTimeField(auto_now_add=True, blank=True)
	status = models.CharField(
		max_length=20,
		choices=(
			('DRAFT', 'Draft'),
			('UNPAID', "Waiting for payment"),
			('PAID', "Paid"),
		),
		default="DRAFT"
	)
	paid = models.BooleanField(default=False) # fixme - remove this eventually
	opened = models.BooleanField(default=False)
	email_sent = models.BooleanField(default=False)
	seller_email_sent = models.BooleanField(default=False)
	adjustment_direction = models.CharField(
		max_length=20,
		choices=(
			('PAY_SELLER', 'Discount'),
			('PAY_CLUB', "Charge extra"),
		),
		default="PAY_CLUB"
	)
	adjustment = models.PositiveIntegerField(default = 0, validators=[MinValueValidator(0)])
	adjustment_notes = models.CharField(max_length=150, default="Corrected")

	@property
	def first_bid_payout(self):
		try:
			if self.auction.first_bid_payout:
				if self.lots_bought:
					return self.auction.first_bid_payout
		except:
			pass
		return 0

	@property
	def net(self):
		"""Factor in:
		Total bought
		Total sold
		Any auction-wide payout promotions
		Any manual adjustments made to this invoice
		"""
		subtotal = self.total_sold - self.total_bought
		# if this auction is using the first bid payout system to encourage people to bid
		subtotal += self.first_bid_payout
		if self.adjustment:
			if self.adjustment_direction == 'PAY_SELLER':
				subtotal += self.adjustment
			else:
				subtotal -= self.adjustment
		if not subtotal:
			subtotal = 0
		return subtotal

	@property
	def user_should_be_paid(self):
		"""Return true if the user owes the club money.  Most invoices will be negative unless the user is a vendor"""
		if self.net > 0:
			return True
		else:
			return False

	@property
	def rounded_net(self):
		"""Always round in the customer's favor (against the club) to make sure that the club doens't need to deal with change, only whole dollar amounts"""
		rounded = round(self.net)
		#print(f"{self.net} Rounded to {rounded}")
		if self.user_should_be_paid:
			if self.net > rounded:
				# we rounded down against the customer
				return rounded + 1
			else:
				return rounded
		else:
			if self.net <= rounded:
				return rounded
			else:
				return rounded + 1

	@property
	def absolute_amount(self):
		"""Give the absolute value of the invoice's net amount"""
		return abs(self.rounded_net)

	@property
	def lots_sold(self):
		"""Return number of lots the user attempted to sell in this invoice (unsold lots included)"""
		return len(Lot.objects.filter(seller_invoice=self.pk))#:

	@property
	def lots_sold_successfully(self):
		"""Return number of lots the user sold in this invoice (unsold lots not included)"""
		return len(Lot.objects.filter(seller_invoice=self.pk, winner__isnull=False))


	@property
	def total_sold(self):
		"""Seller's cut of all lots sold"""
		allSold = Lot.objects.filter(seller_invoice=self.pk)
		total_sold = 0
		for lot in allSold:
			total_sold += lot.your_cut
		return total_sold

	@property
	def lots_bought(self):
		"""Return number of lots the user bought in this invoice"""
		return len(Lot.objects.filter(buyer_invoice=self.pk))

	@property
	def total_bought(self):
		allSold = Lot.objects.filter(buyer_invoice=self.pk)
		total_bought = 0
		for lot in allSold:
			if lot.winning_price:
				total_bought += lot.winning_price
		return total_bought

	@property
	def location(self):
		"""Pickup location selected by the user"""
		try:
			return AuctionTOS.objects.get(user=self.user.pk, auction=self.auction.pk).pickup_location
		except:
			return "No location selected"

	@property
	def invoice_summary(self):
		base = str(self.user.first_name)
		if self.user_should_be_paid:
			base += " needs to be paid"
		else:
			base += " owes the club"
		return base + " $" + "%.2f" % self.absolute_amount

	@property
	def label(self):
		if self.auction:
			return self.auction
		if self.seller:
			dateString = self.date.strftime("%b %Y")
			return f"{self.seller} {dateString}"
		return "Unknown"

	def __str__(self):
		if self.auction:
			return f"{self.user}'s invoice for {self.auction}"
		else:
			return f"{self.user}'s invoice from {self.seller}"
		#base = str(self.user)
		#if self.user_should_be_paid:
		#	base += " needs to be paid"
		#else:
		#	base += " owes the club"
		#return base + " $" + "%.2f" % self.absolute_amount

class Bid(models.Model):
	"""Bids apply to lots"""
	user = models.ForeignKey(User, on_delete=models.CASCADE)
	lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
	bid_time = models.DateTimeField(auto_now_add=True, blank=True)
	last_bid_time = models.DateTimeField(auto_now_add=True, blank=True)
	amount = models.PositiveIntegerField(validators=[MinValueValidator(1)])
	was_high_bid = models.BooleanField(default=False)

	def __str__(self):
		return str(self.user) + " bid " + str(self.amount) + " on lot " + str(self.lot_number)

class Watch(models.Model):
	"""
	Users can watch lots.
	This adds them to a list on the users page, and sends an email 2 hours before the auction ends
	"""
	user = models.ForeignKey(User, on_delete=models.CASCADE)
	lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
	def __str__(self):
		return str(self.user) + " watching " + str(self.lot_number)
	class Meta:
		verbose_name_plural = "Users watching"


class UserBan(models.Model):
	"""
	Users can ban other users from bidding on their lots
	This will prevent the banned_user from bidding on any lots or in auction auctions created by the owned user
	"""
	user = models.ForeignKey(User, on_delete=models.CASCADE)
	banned_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='banned_user')
	def __str__(self):
		return str(self.user) + " has banned " + str(self.banned_user)

class UserIgnoreCategory(models.Model):
	"""
	Users can choose to hide all lots from all views
	"""
	user = models.ForeignKey(User, on_delete=models.CASCADE)
	category = models.ForeignKey(Category, on_delete=models.CASCADE)
	def __str__(self):
		return str(self.user) + " hates " + str(self.category)


class PageView(models.Model):
	"""Track what lots a user views, and how long they spend looking at each one"""
	user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
	lot_number = models.ForeignKey(Lot, null=True, blank=True, on_delete=models.CASCADE)
	blog_post = models.ForeignKey(BlogPost, null=True,blank=True, on_delete=models.CASCADE) # fixme remove this
	date_start = models.DateTimeField(auto_now_add=True)
	date_end = models.DateTimeField(null=True,blank=True)
	total_time = models.PositiveIntegerField(default=0)
	total_time.help_text = 'The total time in seconds the user has spent on the lot page'

	def __str__(self):
		if self.blog_post:
			thing = self.blog_post.title
		else:
			thing = self.lot_number
		return f"User {self.user} viewed {thing} for {self.total_time} seconds"

class UserData(models.Model):
	"""
	Extension of user model to store additional info
	"""
	user = models.OneToOneField(User, on_delete=models.CASCADE)
	phone_number = models.CharField(max_length=20, blank=True, null=True)
	address = models.CharField(max_length=500, blank=True, null=True)
	address.help_text="Your complete mailing address.  If you sell lots in an auction, your check will be mailed here."
	location = models.ForeignKey(Location, blank=True, null=True, on_delete=models.SET_NULL)
	club = models.ForeignKey(Club, blank=True, null=True, on_delete=models.SET_NULL)
	use_list_view = models.BooleanField(default=False)
	use_list_view.help_text = "Show a list of all lots instead of showing pictures"
	email_visible = models.BooleanField(default=True)
	email_visible.help_text = "Show your email address on your user page.  This will be visible only to logged in users.  <a href='/blog/privacy/' target='_blank'>Privacy information</a>"
	last_auction_used = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
	last_activity = models.DateTimeField(auto_now_add=True)
	latitude = models.FloatField(blank=True, null=True)
	longitude = models.FloatField(blank=True, null=True)
	location_coordinates = PlainLocationField(based_fields=['address'], zoom=11, blank=True, null=True, verbose_name="Map")
	location_coordinates.help_text = "Make sure your map marker is correctly placed - you will get notifications about nearby auctions"
	email_me_about_new_auctions = models.BooleanField(default=True, blank=True)
	email_me_about_new_auctions.help_text = "When new auctions are created with pickup locations near my location, notify me"
	email_me_about_new_auctions_distance = models.PositiveIntegerField(null=True, blank=True, default=100, verbose_name="New auction distance")
	email_me_about_new_auctions_distance.help_text = "miles, from your address"
	email_me_about_new_local_lots = models.BooleanField(default=True, blank=True)
	email_me_about_new_local_lots.help_text = "When new nearby lots (that aren't part of an auction) are created, notify me"
	local_distance = models.PositiveIntegerField(null=True, blank=True, default=60, verbose_name="New local lot distance")
	local_distance.help_text = "miles, from your address"
	email_me_about_new_lots_ship_to_location = models.BooleanField(default=True, blank=True, verbose_name="Email me about lots that can be shipped")
	email_me_about_new_lots_ship_to_location.help_text = "Email me when new lots are created that can be shipped to my location"
	paypal_email_address = models.CharField(max_length=200, blank=True, null=True)
	paypal_email_address.help_text = "This is your Paypal address, if different from your email address"
	unsubscribe_link = models.CharField(max_length=255, default=uuid.uuid4, blank=True)
	has_unsubscribed = models.BooleanField(default=False, blank=True)
	banned_from_chat_until = models.DateTimeField(null=True, blank=True)
	banned_from_chat_until.help_text = "After this date, the user can post chats again.  Being banned from chatting does not block bidding"

	# breederboard info
	rank_unique_species = models.PositiveIntegerField(null=True, blank=True)
	number_unique_species = models.PositiveIntegerField(null=True, blank=True)
	rank_total_lots = models.PositiveIntegerField(null=True, blank=True)
	number_total_lots = models.PositiveIntegerField(null=True, blank=True)
	rank_total_spent = models.PositiveIntegerField(null=True, blank=True)
	number_total_spent = models.PositiveIntegerField(null=True, blank=True)
	rank_total_bids = models.PositiveIntegerField(null=True, blank=True)
	number_total_bids = models.PositiveIntegerField(null=True, blank=True)
	number_total_sold = models.PositiveIntegerField(null=True, blank=True)
	rank_total_sold = models.PositiveIntegerField(null=True, blank=True)
	total_volume = models.PositiveIntegerField(null=True, blank=True)
	rank_volume = models.PositiveIntegerField(null=True, blank=True)
	seller_percentile = models.PositiveIntegerField(null=True, blank=True)
	buyer_percentile = models.PositiveIntegerField(null=True, blank=True)
	volume_percentile = models.PositiveIntegerField(null=True, blank=True)

	def __str__(self):
		return f"{self.user.username}'s data"

	@property
	def lots_submitted(self):
		"""All lots this user has submitted, including unsold"""
		allLots = Lot.objects.filter(user=self.user)
		return len(allLots)

	@property
	def lots_sold(self):
		"""All lots this user has sold"""
		allLots = Lot.objects.filter(user=self.user,winner__isnull=False)
		return len(allLots)

	@property
	def total_sold(self):
		"""Total amount this user has spent on this site"""
		allLots = Lot.objects.filter(user=self.user.pk)
		total = 0
		for lot in allLots:
			try:
				total += lot.winning_price
			except:
				pass
		return total

	@property
	def species_sold(self):
		"""Total different species that this user has bred and sold in auctions"""
		allLots = Lot.objects.filter(user=self.user,i_bred_this_fish=True,winner__isnull=False).values('species').distinct().count()
		return allLots

	@property
	def lots_bought(self):
		"""Total number of lots this user has purchased"""
		allLots = Lot.objects.filter(winner=self.user)
		return len(allLots)
	
	@property
	def total_spent(self):
		"""Total amount this user has spent on this site"""
		allLots = Lot.objects.filter(winner=self.user)
		total = 0
		for lot in allLots:
			total += lot.winning_price
		return total

	@property
	def calc_total_volume(self):
		"""Bought + sold"""
		return self.total_spent + self.total_sold

	@property
	def total_bids(self):
		"""Total number of successful bids this user has placed (max one per lot)"""
		#return len(Bid.objects.filter(user=self.user, was_high_bid=True))
		return len(Bid.objects.filter(user=self.user))

	@property
	def lots_viewed(self):
		"""Total lots viewed by this user"""
		return len(PageView.objects.filter(user=self.user.pk))
	
	@property
	def bought_to_sold(self):
		"""Ratio of lots bought to lots sold"""
		if self.lots_sold:
			return self.lots_bought / self.lots_sold
		else:
			return 0
	
	@property
	def bid_to_view(self):
		"""Ratio of lots viewed to lots bought.  Lower number is indicative of tire kicking, higher number means business"""
		if self.lots_viewed:
			return self.total_bids / self.lots_viewed 
		else:
			return 0

	@property
	def viewed_to_sold(self):
		"""Ratio of lots viewed to lots sold"""
		if self.lots_viewed:
			return self.lots_sold / self.lots_viewed
		else:
			return 0

	@property
	def dedication(self):
		"""Ratio of bids to won lots"""
		if self.lots_bought:
			return self.lots_bought / self.total_bids
		else:
			return 0
	
	@property
	def percent_success(self):
		"""Ratio of bids to won lots, formatted"""
		return self.dedication * 100
	
	@property
	def positive_feedback_as_seller(self):
		feedback = Lot.objects.filter(user=self.user.pk, feedback_rating=1).count()
		return feedback

	@property
	def negative_feedback_as_seller(self):
		feedback = Lot.objects.filter(user=self.user.pk, feedback_rating=-1).count()
		return feedback

	@property
	def percent_positive_feedback_as_seller(self):
		positive = self.positive_feedback_as_seller
		negative = self.negative_feedback_as_seller
		if not negative:
			return 100
		return int(( positive / (positive + negative) ) * 100)

	@property
	def positive_feedback_as_winner(self):
		feedback = Lot.objects.filter(winner=self.user.pk, winner_feedback_rating=1).count()
		return feedback

	@property
	def negative_feedback_as_winner(self):
		feedback = Lot.objects.filter(winner=self.user.pk, winner_feedback_rating=-1).count()
		return feedback

	@property
	def percent_positive_feedback_as_winner(self):
		positive = self.positive_feedback_as_winner
		negative = self.negative_feedback_as_winner
		if not negative:
			return 100
		return int(( positive / (positive + negative) ) * 100)

class UserInterestCategory(models.Model):
	"""
	How interested is a user in a given category
	"""
	user = models.ForeignKey(User, on_delete=models.CASCADE)
	category = models.ForeignKey(Category, on_delete=models.CASCADE)
	interest = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])

	def __str__(self):
		return f"{self.user} interest level in {self.category} is {self.interest}"

	@property
	def as_percent(self):
		"""Rank of this interest relative to all of this user's interests"""
		try:
			max = UserInterestCategory.objects.filter(user=self.user).order_by('-interest')[0].interest
			return int((self.interest / max) * 100)
		except:
			return 100

class LotHistory(models.Model):
	lot = models.ForeignKey(Lot, blank=True, null=True, on_delete=models.CASCADE)
	user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	user.help_text = "The user who posted this message."
	message = models.CharField(max_length=400, blank=True, null=True)
	timestamp = models.DateTimeField(auto_now_add=True)
	seen = models.BooleanField(default=False)
	seen.help_text = "Has the lot submitter seen this message?"
	current_price = models.PositiveIntegerField(null=True, blank=True)
	current_price.help_text = "Price of the lot immediately AFTER this message"
	changed_price = models.BooleanField(default=False)
	changed_price.help_text = "Was this a bid that changed the price?"
	
	def __str__(self):
		if self.message:
			return f"{self.message}"
		else:
			return "message"

	class Meta:
		verbose_name_plural = "Chat history"
		verbose_name = "Chat history"
		ordering = ['timestamp']


@receiver(pre_save, sender=UserData)
@receiver(pre_save, sender=PickupLocation)
def update_user_location(sender, instance, **kwargs):
	"""
	GeoDjango does not appear to support MySQL and Point objects well at the moment (2020)
	To get around this, I'm storing the coordinates in a raw latitude and longitude column

	The custom function distance_to is used to annotate queries

	It is bad practice to use a signal in models.py,
	however with just a couple signals it makes more sense to have them here than to add a whole separate file for it
	"""
	try:
		cutLocation = instance.location_coordinates.split(',')
		instance.latitude = float(cutLocation[0])
		instance.longitude = float(cutLocation[1])
	except:
		pass

@receiver(pre_save, sender=Lot)
def update_lot_info(sender, instance, **kwargs):
	"""
	Fill out the location and address from the user
	Fill out end date from the auction
	"""
	if instance.auction:
		instance.date_end = instance.auction.date_end
	userData, created = UserData.objects.get_or_create(
		user = instance.user,
		defaults={},
		)
	instance.latitude = userData.latitude
	instance.longitude = userData.longitude
	instance.address = userData.address

def get_recommended_lots(
		user=None,
		local=False,
		location=None,
		auction=None,
		latitude=None,
		longitude=None,
		distance=None, 
		onlyUnseen=False,
		onlyActive=False,
		qty=10):
	"""
	This is the core of the recommendation system - it's very expensive to run since it loops over almost all lots.
	Returns a queryset of lot objects ready for use in a template.
	"""
	# start with everything
	qs = Lot.objects.filter(banned=False)
	# if an auction is specified, only show stuff from that auction
	if auction:
		qs = qs.filter(auction__slug=auction)
	else:
		qs = qs.filter(auction__isnull=True)
	if local:
		if latitude and longitude and distance:
			qs = qs.annotate(distance=distance_to(latitude, longitude)).filter(local_pickup=True, auction__isnull=True, distance__lte=distance)
		else:
			print('error in get_recommended_lots - local requires latitude, longitude, and distance to be set')
			return []
	if location:
		# show only lots that are shippable to the specified location
		location = Location.objects.get(pk=location)
		qs = qs.filter(shipping_locations=location, auction__isnull=True)
	if onlyActive:
		qs = qs.filter(active=True)
	if user:
		try:
			user = User.objects.get(pk=user)
			# don't show users their own lots
			qs = qs.exclude(user=user.pk)
			# and exclude any ignored categories
			allowedCategories = Category.objects.exclude(userignorecategory__user=user.pk)
			qs = qs.filter(species_category__in=allowedCategories)
		except:
			print("error in get_recommended_lots - user must be the pk of a user")
			user = None
	if user and onlyUnseen:
		# exclude viewed lots
		qs = qs.exclude(pageview__user=user.pk)
	# all parameters accounted for, here's the actual logic
	count = len(qs)
	result = []
	if not count:
		return []
	timeout = 0 # hard cap to prevent an infinite loop
	if count <= qty:
		# if there's only a few lots, just return them all
		return qs
	while len(result) < qty and timeout < qty*4:
		# select a random lot
		resultLot = qs[randint(0, count - 1)]
		if resultLot not in result: # no duplicates
			shouldAdd = False
			if user:
				# weight the chance to see a given lot based on the user's history
				try:
					interest = UserInterestCategory.objects.get(category=resultLot.species_category, user=user.pk).as_percent
				except:
					interest = 10 # low chance to view lots that the user has no history for
				rand = randint(0, 100-settings.WEIGHT_AGAINST_TOP_INTEREST)
				#if resultLot.promoted:
				if True: # temporarily, we need to collect data on all lots
					interest = interest + resultLot.promotion_weight
				if interest > rand:
					shouldAdd = True
					resultLot.your_interest = interest
			else:
				# just show random lots to non-authenticated users
				shouldAdd = True
			if shouldAdd:
				result.append(resultLot)
		timeout += 1
	return result