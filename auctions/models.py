import decimal
from django.utils import timezone
import datetime
from django.contrib.auth.models import *
from django.db import models
from django.core.validators import *
from django.db.models import Count
from autoslug import AutoSlugField
from django.urls import reverse

class Club(models.Model):
	"""Clubs restrict who can enter or bid in an auction"""
	name = models.CharField(max_length=255)
	def __str__(self):
		return str(self.name)
	# fixme - create a new group model to allow adding users to a club
	# fixme - create a new auction permission model to restrict bidding in an auction to a given club

class Category(models.Model):
	"""Picklist of species.  Used for product, lot, and interest"""
	name = models.CharField(max_length=255)
	def __str__(self):
		return str(self.name)
	class Meta:
		verbose_name_plural = "Categories"

class Product(models.Model):
	"""A species or item in the auction"""
	common_name = models.CharField(max_length=255)
	common_name.help_text = "The name usually used to describe this species"
	scientific_name = models.CharField(max_length=255, blank=True)
	scientific_name.help_text = "Latin name used to describe this species"
	breeder_points = models.BooleanField(default=True)
	category = models.ForeignKey(Category, null=True, on_delete=models.SET_NULL)
	def __str__(self):
		return str(self.common_name)
	class Meta:
		verbose_name_plural = "Products and species"

class Auction(models.Model):
	"""An auction is a collection of lots"""
	title = models.CharField(max_length=255)
	slug = AutoSlugField(populate_from='title', unique=True)
	sealed_bid = models.BooleanField(default=False)
	lot_entry_fee = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
	lot_entry_fee.help_text = "The amount, in dollars, that the seller will be charged if a lot sells"
	unsold_lot_fee = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
	unsold_lot_fee.help_text = "The amount, in dollars, that the seller will be charged if their lot doesn't sell"
	winning_bid_percent_to_club = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
	winning_bid_percent_to_club.help_text = "To give 70% of the final bid to the seller, enter 30 here"
	date_start = models.DateTimeField()
	date_end = models.DateTimeField()
	watch_warning_email_sent = models.BooleanField(default=False)
	invoiced = models.BooleanField(default=False)
	created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	area = models.CharField(max_length=300)
	area.help_text = "State or region of this auction"
	pickup_location = models.CharField(max_length=300)
	pickup_location.help_text = "Description of pickup location"
	pickup_location_map = models.CharField(max_length=2000)
	pickup_location_map.help_text = "Find the location on Google maps, click Menu>Share or Embed Map and paste the embed link here"
	pickup_time = models.DateTimeField()
	alternate_pickup_location = models.CharField(null=True, blank=True, max_length=300)
	alternate_pickup_location.help_text = "Description of alternate pickup location"
	alternate_pickup_location_map = models.CharField(null=True, blank=True, max_length=2000)
	alternate_pickup_location_map.help_text = "Google Maps link to alternate pickup location"
	alternate_pickup_time = models.DateTimeField(blank=True, null=True)
	notes = models.CharField(max_length=500, blank=True, null=True)
	code_to_add_lots = models.CharField(max_length=255, blank=True, null=True)
	code_to_add_lots.help_text = "This is like a password: People in your club will enter this code to put their lots in this auction"

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
		warning_date = self.date_end
		if timezone.now() > warning_date:
			return True
		else:
			return False

class Invoice(models.Model):
	"""An invoice is applied to an auction.  It's the total amount you owe"""
	auction = models.ForeignKey(Auction, blank=True, null=True, on_delete=models.SET_NULL)
	user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
	date = models.DateTimeField(auto_now_add=True, blank=True)
	sold = models.TextField(blank=True)
	total_sold = models.DecimalField(default=0, max_digits=10, decimal_places=2)
	bought = models.TextField(blank=True)
	total_bought = models.DecimalField(default=0, max_digits=10, decimal_places=2)
	paid = models.BooleanField(default=False)
	email_sent = models.BooleanField(default=True) # we will set to false manually in the admin console
	@property
	def net(self):
		return self.total_bought - self.total_sold

	def __str__(self):
		#return "ID:" + str(self.pk) + " " + str(self.title)
		base = str(self.auction) + " - " + str(self.user)
		if self.net < 0:
			return base + " needs to be paid $" + str(abs(self.net))
		else:
			return base + " owes the club $" + str(self.net)


class Lot(models.Model):
	"""A lot is something to bid on"""
	PIC_CATEGORIES = (
		('ACTUAL', 'This picture is of the exact item'),
		('REPRESENTATIVE', "This is my picture, but it's not of this exact item.  e.x. This is the parents of these fry"),
		('RANDOM', 'This picture is from the internet'),
	)
	lot_number = models.AutoField(primary_key=True)
	lot_name = models.CharField(max_length=255, default="")
	lot_name.help_text = "Species name or common name"
	image = models.ImageField(upload_to='images/', blank=True)
	image.help_text = "Add a picture of the item here"
	image_source = models.CharField(
		max_length=20,
		choices=PIC_CATEGORIES,
		blank=True
	)
	image_source.help_text = "Where did you get this image?"
	i_bred_this_fish = models.BooleanField(default=False)
	i_bred_this_fish.help_text = "Check to get breeder points for this lot"
	description = models.CharField(max_length=500, blank=True, null=True)
	#description.help_text = "Enter a detailed description of this lot"
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
	donation = models.BooleanField(default=False)
	donation.help_text = "All proceeds from this lot should go to the auction"
	watch_warning_email_sent = models.BooleanField(default=False)
	seller_invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="seller_invoice")
	buyer_invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="buyer_invoice")

	class Meta:
		unique_together = (('user', 'active', 'lot_name', 'description'),)

	def __str__(self):
		return "" + str(self.lot_number) + " - " + self.lot_name

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
				auction = Auction.objects.get(id=self.auction.pk)
				if self.winner:
					# this lot sold
					payout['sold'] = True
					payout['winning_price'] = self.winning_price
					if not self.donation:
						clubCut = ( self.winning_price * auction.winning_bid_percent_to_club / 100 ) + auction.lot_entry_fee
						sellerCut = self.winning_price - clubCut
					else:
						clubCut = self.winning_price
						sellerCut = 0
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
		else:
			return self.date_end
	
	@property
	def ended(self):
		"""Used by the view for display of whether or not the auction has ended
		See also the database field active, which is set by a system job"""
		if timezone.now() > self.calculated_end:
			return True
		else:
			return False

	@property
	def max_bid(self):
		"""returns the highest bid amount for this lot - this number should not be visible to the public"""
		allBids = Bid.objects.filter(lot_number=self.lot_number, bid_time__lte=self.calculated_end, amount__gte=self.reserve_price).order_by('-amount')[:2]
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
		#allBids = Bid.objects.filter(lot_number=self.lot_number).exclude(bid_time__gt=self.calculated_end).exclude(amount__lt=self.reserve_price).order_by('-amount')[:2]
		allBids = Bid.objects.filter(lot_number=self.lot_number, bid_time__lte=self.calculated_end, amount__gte=self.reserve_price).order_by('-amount')[:2]
		# highest bid is the winner, but the second highest determines the price
		try:
			# $1 more than the second highest bid
			bidPrice = allBids[1].amount + 1
			return bidPrice
		except:
			#print("no bids for this item")
			return self.reserve_price

	@property
	def high_bidder(self):
		""" Name of the highest bidder """
		allBids = Bid.objects.filter(lot_number=self.lot_number, bid_time__lte=self.calculated_end, amount__gte=self.reserve_price).order_by('-amount')[:2]
		try:
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

class Bid(models.Model):
	"""Bids apply to lots"""
	user = models.ForeignKey(User, on_delete=models.CASCADE)
	lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
	bid_time = models.DateTimeField(auto_now_add=True, blank=True)
	amount = models.PositiveIntegerField(validators=[MinValueValidator(1)])
	
	def __str__(self):
		return "User" + str(self.user) + " bid " + str(self.amount) + " on lot " + str(self.lot_number)

class Watch(models.Model):
	"""
	Users can watch lots.
	This adds them to a list on the users page, and sends an email 2 hours before the auction ends
	"""
	user = models.ForeignKey(User, on_delete=models.CASCADE)
	lot_number = models.ForeignKey(Lot, on_delete=models.CASCADE)
	def __str__(self):
		return "User" + str(self.user) + " watching " + str(self.lot_number)
