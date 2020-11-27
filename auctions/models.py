import decimal
from django.utils import timezone
import datetime
from django.contrib.auth.models import *
from django.db import models
from django.core.validators import *
from django.db.models import Count, Sum
from autoslug import AutoSlugField
from django.urls import reverse
from django.db.models import Q
from markdownfield.models import MarkdownField, RenderedMarkdownField
from markdownfield.validators import VALIDATOR_STANDARD
from easy_thumbnails.fields import ThumbnailerImageField

def median_value(queryset, term):
    count = queryset.count()
    return queryset.values_list(term, flat=True).order_by(term)[int(round(count/2))]

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
	date_start = models.DateTimeField()
	lot_submission_end_date = models.DateTimeField(null=True, blank=True)
	date_end = models.DateTimeField()
	watch_warning_email_sent = models.BooleanField(default=False)
	invoiced = models.BooleanField(default=False)
	created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	location = models.CharField(max_length=300, null=True, blank=True)
	location.help_text = "State or region of this auction"
	notes = MarkdownField(rendered_field='notes_rendered', validator=VALIDATOR_STANDARD, blank=True, null=True)
	notes.help_text = "To add a link: [Link text](https://www.google.com)"
	notes_rendered = RenderedMarkdownField(blank=True, null=True)
	code_to_add_lots = models.CharField(max_length=255, blank=True, null=True)
	code_to_add_lots.help_text = "This is like a password: People in your club will enter this code to put their lots in this auction"
	lot_promotion_cost = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
	first_bid_payout = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
	first_bid_payout.help_text = "The first time a user bids in this auction, give them a credit in this amount.  This will appear on their invoice"

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
		#return "ID:" + str(self.pk) + " " + str(self.title)
		return str(self.title)
	
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
		return self.total_unsold_lots / self.total_lots * 100
	
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

class PickupLocation(models.Model):
	"""
	A pickup location associated with an auction
	A given auction can have multiple pickup locations
	"""
	name = models.CharField(max_length=50, default="")
	user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	auction = models.ForeignKey(Auction, null=True, blank=True, on_delete=models.CASCADE)
	description = models.CharField(max_length=300)
	description.help_text = "e.x. First floor of parking garage near Sears entrance"
	google_map_iframe = models.CharField(max_length=2000, blank=True, null=True)
	google_map_iframe.help_text = "Find the location on Google maps, click Menu>Share or Embed Map and paste the embed link here.  You must embed an iframe, not a link."
	pickup_time = models.DateTimeField()
	second_pickup_time = models.DateTimeField(blank=True, null=True)
	second_pickup_time.help_text = "If you'll have a dropoff for sellers in the morning and then a pickup for buyers in the afternoon at this location, this should be the pickup time."
	
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
	lot_name = models.CharField(max_length=255, default="")
	lot_name.help_text = "Short description of this lot"
	image = ThumbnailerImageField(upload_to='images/', blank=True)
	image.help_text = "Add a picture of the item here"
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
	reserve_price.help_text = "The item will not be sold unless someone bids at least this much"
	species = models.ForeignKey(Product, null=True, blank=True, on_delete=models.SET_NULL)
	species_category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL)
	date_posted = models.DateTimeField(auto_now_add=True, blank=True)
	user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	auction = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
	auction.help_text = "Select an auction to put this lot into.  This lot must be brought to the auction's pickup location"
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
	promoted = models.BooleanField(default=False)
	promotion_weight = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(20)])
	
	def __str__(self):
		return "" + str(self.lot_number) + " - " + self.lot_name

	@property
	def winner_location(self):
		"""Model object of location of the winner for this lot, or False"""
		# try:
		# 	return UserData.objects.get(user=self.winner.pk).location
		# except:
		# 	return ""
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
		"""Model object of location of the user for this lot, or False"""
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
			auction = Auction.objects.get(id=self.auction.pk)
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
	def ending_soon(self):
		"""Used to send notifications"""
		warning_date = self.calculated_end - datetime.timedelta(hours=2)
		if timezone.now() > warning_date:
			return True
		else:
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

class Invoice(models.Model):
	"""
	An invoice is applied to an auction
	or a lot (if the lot is not associated with an auction)
	
	It's the total amount you owe and how much you owe to the club
	Invoices get rounded to the nearest dollar
	"""
	auction = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
	lot = models.ForeignKey(Lot, blank=True, null=True, on_delete=models.SET_NULL)
	user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	date = models.DateTimeField(auto_now_add=True, blank=True)
	paid = models.BooleanField(default=False)
	opened = models.BooleanField(default=False)
	email_sent = models.BooleanField(default=True) # we will set to false manually in the admin console
	adjustment_direction = models.CharField(
		max_length=20,
		choices=(
			('PAY_SELLER', 'Pay the seller'),
			('PAY_CLUB', "Charge the seller"),
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
			total_bought += lot.winning_price
		return total_bought

	@property
	def location(self):
		"""Pickup location selected by the user"""
		return AuctionTOS.objects.get(user=self.user.pk, auction=self.auction.pk).pickup_location

	def __str__(self):
		base = str(self.user)
		if self.user_should_be_paid:
			base += " needs to be paid"
		else:
			base += " owes the club"
		return base + " $" + "%.2f" % self.absolute_amount

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
	lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
	date_start = models.DateTimeField(auto_now_add=True)
	date_end = models.DateTimeField(null=True,blank=True)
	total_time = models.PositiveIntegerField(default=0)
	total_time.help_text = 'The total time in seconds the user has spent on the lot page'

	def __str__(self):
		return f"User {self.user} viewed {self.lot_number} for {self.total_time} seconds"

class UserData(models.Model):
	"""Extension of user model to store additional info.  At some point, we should be able to store information like email preferences here"""
	user = models.OneToOneField(User, on_delete=models.CASCADE)
	phone_number = models.CharField(max_length=20, blank=True, null=True)
	address = models.CharField(max_length=500, blank=True, null=True)
	location = models.ForeignKey(Location, blank=True, null=True, on_delete=models.SET_NULL)
	club = models.ForeignKey(Club, blank=True, null=True, on_delete=models.SET_NULL)
	use_list_view = models.BooleanField(default=False)
	use_list_view.help_text = "Show a list of all lots instead of showing tiles"
	email_visible = models.BooleanField(default=True)
	last_auction_used = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
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
