from allauth.account.forms import SignupForm
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Submit, HTML
from crispy_forms.bootstrap import Div, Field
from django import forms
from .models import Lot, Bid, Auction, User, UserData, Location, Club, PickupLocation, AuctionTOS, Invoice, Category
from django.forms import ModelForm, HiddenInput, RadioSelect, ModelChoiceField
from bootstrap_datepicker_plus import DateTimePickerInput
from django.utils import timezone
from location_field.models.plain import PlainLocationField
#from django.core.exceptions import ValidationError

class DateInput(forms.DateInput):
    input_type = 'datetime-local'

class CreateBid(forms.ModelForm):
    #amount = forms.IntegerField()
    def __init__(self, *args, **kwargs):
        self.req = kwargs.pop('request', None)
        self.lot = kwargs.pop('lot', None)
        super().__init__(*args, **kwargs)
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_class = 'form-inline'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'user',
            'lot_number',
            'amount',
            Submit('submit', 'Place bid', css_class='place-bid btn-success'),
        )
        self.fields['user'].widget = HiddenInput()
        self.fields['lot_number'].widget = HiddenInput()
        
    # def save(self, *args, **kwargs):
    #     kwargs['commit']=False
    #     obj = super().save(*args, **kwargs)
    #     print(self.req.user.id)
    #     #obj.user = self.req.user.id
    #     #print(str(obj.user)+ " has placed a bid on " + str(obj.lot_number))
    #     obj.save()
    #     return obj

    class Meta:
        model = Bid
        fields = [
            'user',
            'lot_number',
            'amount',
        ]

class InvoiceUpdateForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_class = 'form-inline'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'adjustment_direction',
            'adjustment',
            'adjustment_notes',
            Submit('submit', 'Update', css_class='btn-primary'),
        )
        self.fields['adjustment_direction'].label = ""
        self.fields['adjustment'].label = " $"
        self.fields['adjustment_notes'].label = "Reason"
        
    class Meta:
        model = Invoice
        fields = [
            'adjustment_direction',
            'adjustment',
            'adjustment_notes',
        ]

class AuctionTOSForm(forms.ModelForm):
    i_agree = forms.BooleanField(required = True)

    def __init__(self, user, auction, *args, **kwargs):
        self.user = user
        self.auction = auction
        super().__init__(*args, **kwargs)
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_class = 'form-inline'
        self.helper.form_tag = True        
        self.helper.layout = Layout(
            'i_agree',
            #'auction',
            'pickup_location',
            Submit('submit', 'Confirm pickup location and view lots', css_class='agree_tos btn-success'),
        )
        self.fields['pickup_location'].queryset = PickupLocation.objects.filter(auction=self.auction).order_by('name')
        if self.auction.multi_location:
            self.fields['i_agree'].initial = True
            self.fields['i_agree'].widget = HiddenInput()
            self.fields['pickup_location'].label = "Yes, I will be at &nbsp;&nbsp;&nbsp;"
        else:
            # single location auction
            self.fields['pickup_location'].widget = HiddenInput()
            if not self.auction.no_location:
                self.fields['pickup_location'].initial = PickupLocation.objects.filter(auction=self.auction)[0]
                self.fields['i_agree'].label = f"Yes, I will be at {PickupLocation.objects.filter(auction=self.auction)[0]}"

    class Meta:
        model = AuctionTOS
        fields = [
            'pickup_location',
        ]
        exclude = ['user', 'auction',]


class PickupLocationForm(forms.ModelForm):
    class Meta:
        model = PickupLocation
        fields = ['name', 'auction', 'description',  'pickup_time', 'second_pickup_time', 'address', \
            'location_coordinates', 'pickup_location_contact_name', 'pickup_location_contact_phone', \
            'pickup_location_contact_email', 'users_must_coordinate_pickup']
        exclude = ['user', 'latitude', 'longitude',]
        widgets = {
            'pickup_time': DateTimePickerInput(),
            'second_pickup_time': DateTimePickerInput(),
            'description': forms.Textarea,
        }
    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['description'].widget.attrs = {'rows': 3}
        self.fields['auction'].queryset = Auction.objects.filter(created_by=self.user).filter(date_end__gte=timezone.now()).order_by('date_end')
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_id = 'location-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'name',
            'description',
            'auction',
            HTML("<h4>Contact info</h4>"),
            Div(
                Div('pickup_location_contact_name',css_class='col-md-6',),
                Div('pickup_location_contact_phone',css_class='col-md-6',),
                Div('pickup_location_contact_email',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('users_must_coordinate_pickup',css_class='col-md-4',),
                Div('pickup_time',css_class='col-md-4',),
                Div('second_pickup_time',css_class='col-md-4',),
                css_class='row',
            ),
            'address',
            'location_coordinates',
            Submit('submit', 'Save', css_class='btn-success'),
        )
    
    def clean(self):
        cleaned_data = super().clean()
        auction = cleaned_data.get("auction")
        if auction:
            if self.user.pk == auction.created_by.pk:
                pass
            else:
                self.add_error('auction', "You can only add pickup locations to your own auctions")

class CreateAuctionForm(forms.ModelForm):
    class Meta:
        model = Auction
        fields = ['title', 'notes', 'lot_entry_fee','unsold_lot_fee','winning_bid_percent_to_club', 'date_start', 'date_end', \
            'lot_submission_end_date', 'first_bid_payout', 'sealed_bid','promote_this_auction']
        exclude = ['slug', 'watch_warning_email_sent', 'invoiced', 'created_by', 'code_to_add_lots', \
            'pickup_location', 'pickup_location_map', 'pickup_time', 'alternate_pickup_location', 'alternate_pickup_location_map',\
            'alternate_pickup_time','location', ]
        widgets = {
            'date_start': DateTimePickerInput(),
            'date_end': DateTimePickerInput(),
            'lot_submission_end_date': DateTimePickerInput(),
            'notes': forms.Textarea,
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['notes'].widget.attrs = {'rows': 3}
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_id = 'auction-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'title',
            'notes',
            Div(
                Div('lot_entry_fee',css_class='col-md-4',),
                Div('winning_bid_percent_to_club',css_class='col-md-4',),
                Div('unsold_lot_fee',css_class='col-md-4',),
                css_class='row',
            ),
            Div(
                Div('date_start',css_class='col-md-4',),
                Div('lot_submission_end_date',css_class='col-md-4',),
                Div('date_end',css_class='col-md-4',),
                css_class='row',
            ),
            Div(
                Div('first_bid_payout', css_class='col-md-4',),
                Div('sealed_bid', css_class='col-md-4',),
                Div('promote_this_auction', css_class='col-md-4',),
                css_class='row',
            ),
            # Div(
            #     Div('pickup_location',css_class='col-md-8',),
            #     Div('pickup_time',css_class='col-md-4',),
            #     css_class='row',
            # ),
            # 'pickup_location_map',
            # Div(
            #     Div('alternate_pickup_location',css_class='col-md-8',),
            #     Div('alternate_pickup_time',css_class='col-md-4',),
            #     css_class='row',
            # ),
            # 'alternate_pickup_location_map',
            Submit('submit', 'Save', css_class='create-update-auction btn-success'),
        )

    def clean(self):
        cleaned_data = super().clean()
        #image = cleaned_data.get("image")
        #image_source = cleaned_data.get("image_source")
        #if image and not image_source:
        #    self.add_error('image_source', "Is this your picture?")

class CreateLotForm(forms.ModelForm):
    """Form for creating or updating of lots"""
    # Fields needed to add new species
    species_search = forms.CharField(max_length=200, required = False)
    species_search.help_text = "Search here for a latin or common name, or the name of a product"
    create_new_species = forms.BooleanField(required = False)
    new_species_name = forms.CharField(max_length=200, required = False, label="Common name")
    new_species_name.help_text = "You can enter synonyms here, separate by commas"
    new_species_scientific_name = forms.CharField(max_length=200, required = False, label="Scientific name")
    new_species_scientific_name.help_text = "Enter the Latin name of this species"
    new_species_category = ModelChoiceField(queryset=Category.objects.all().order_by('name'), required=False,label="Category")
    
    show_payment_pickup_info = forms.BooleanField(required = False, label="Show payment/pickup info")
    AUCTION_CHOICES=[(True,'Yes, this lot is part of a club auction'),
         (False,"No, I'm selling this lot independently")]
    part_of_auction = forms.ChoiceField(choices=AUCTION_CHOICES, widget=forms.RadioSelect, label="Put into an auction?", required = False)
    LENGTH_CHOICES=[(10,'Ends in 10 days'),
         (21,'Ends in 21 days')]
    run_duration = forms.ChoiceField(choices=LENGTH_CHOICES, widget=forms.RadioSelect, label="Posting duration", required = False)

    class Meta:
        model = Lot
        fields = ('lot_name', 'species', 'species_search', 'create_new_species', 'new_species_name', 'new_species_scientific_name',\
            'i_bred_this_fish', 'image','image_source','description','quantity','reserve_price','species_category',\
            'auction','donation', 'shipping_locations', 'buy_now_price', 'show_payment_pickup_info', 'promoted', 'part_of_auction',\
            'other_text', 'local_pickup', 'payment_paypal', 'payment_cash', 'payment_other', 'payment_other_method', 'payment_other_address', 'run_duration','new_species_category')
        exclude = ["user", ]
        widgets = {
            'description': forms.Textarea,
            'species': forms.HiddenInput,
            'shipping_locations':forms.CheckboxSelectMultiple,
        }
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user')
        super().__init__(*args, **kwargs)
        self.fields['description'].widget.attrs = {'rows': 3}
        #self.fields['species_category'].required = True
        self.fields['auction'].queryset = Auction.objects.filter(lot_submission_end_date__gte=timezone.now())\
            .filter(date_start__lte=timezone.now())\
            .filter(auctiontos__user=self.user).order_by('date_end')
        # Default auction selection:
        # try:
        #     auctions = Auction.objects.filter(lot_submission_end_date__gte=timezone.now()).filter(date_start__lte=timezone.now()).order_by('date_end')
        # #    self.fields['auction'].initial = auctions[0] # this would set a default value.  We should make users pick this manually so they don't accidentally submit to the wrong auction
        # except:
        #     # no non-ended auctions
        #     pass
        if self.instance.pk:
            # existing lot
            #set run_duration - this does not have to be super precise as it will be recalculated when the form is validated
            if (self.instance.date_end - self.instance.date_posted).days < 15:
                self.fields['run_duration'].initial = 10
            else:
                self.fields['run_duration'].initial = 21
            self.fields['show_payment_pickup_info'].initial = False # this doesn't really matter, it just gets overridden by javascript anyway
            if self.instance.auction:
                self.fields['part_of_auction'].initial = "True"
            else:
                self.fields['part_of_auction'].initial = "False"
            if self.instance.species:
                self.fields['species_search'].initial = self.instance.species.common_name.split(",")[0]
        else:
            # default to making new lots part of a club auction
            self.fields['part_of_auction'].initial = "True"
            self.fields['run_duration'].initial = 10
            try:
                # try to get the last lot shipping/payment info and use that, set show_payment_pickup_info as needed
                lastLot = Lot.objects.filter(user=self.user, auction__isnull=True).latest("date_of_last_user_edit")
                self.fields['show_payment_pickup_info'].initial = False
                self.fields['shipping_locations'].initial = [place[0] for place in lastLot.shipping_locations.values_list()] 
                self.fields['local_pickup'].initial = lastLot.local_pickup
                self.fields['other_text'].initial = lastLot.other_text
                self.fields['payment_cash'].initial = lastLot.payment_cash
                self.fields['payment_paypal'].initial = lastLot.payment_paypal
                self.fields['payment_other'].initial = lastLot.payment_other
                self.fields['payment_other_method'].initial = lastLot.payment_other_method
                self.fields['payment_other_address'].initial = lastLot.payment_other_address
            except:
                self.fields['show_payment_pickup_info'].initial = True
        if self.instance.auction:
            pass
        else:
            try:
                # see if this user's last auction is still available
                userData, created = UserData.objects.get_or_create(user=self.user)
                lastUserAuction = userData.last_auction_used
                if lastUserAuction.lot_submission_end_date > timezone.now():
                    self.fields['auction'].initial = lastUserAuction
            except:
                pass
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_id = 'lot-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                'species',
                'create_new_species',
                css_class='d-none',
            ),
            HTML("<span id='species_selection'>"),
            HTML("<h4>Species</h4>"),
            HTML('<div class="btn-group" role="group" aria-label="Species Selection">\
                <button id="useExistingSpeciesButton" type="button" onclick="useExistingSpecies();" class="btn btn-secondary selected">Use existing species</button>\
                <button id="createNewSpeciesButton" type="button" onclick="createNewSpecies();" class="btn btn-secondary">Create new species</button>\
                <button id="skipSpeciesButton" type="button" onclick="skipSpecies();" class="btn btn-secondary mr-3">Skip choosing a species</button></div><br>\
                <span class="text-muted">You can search for products as well as species.  If you can\'t find your exact species/morph/collection location, create a new one.<br><br></span>'),
            Div(
                Div('species_search',css_class='col-md-12',),
                css_class='row',
            ),
            Div(
                Div('new_species_name',css_class='col-md-4',),
                Div('new_species_scientific_name',css_class='col-md-4',),
                Div('new_species_category',css_class='col-md-4',),
                Div('species_category',css_class='col-md-12',),
                css_class='row',
            ),
            HTML("</span><span id='details_selection'><h4>Details</h4><br>"),
            Div(
                Div('lot_name',css_class='col-md-12',),
                css_class='row details',
            ),
            Div(
                Div('image',css_class='col-md-8',),
                Div('image_source',css_class='col-md-4',),
                Div('description',css_class='col-md-12',),
                Div('quantity',css_class='col-md-4',),
                Div('i_bred_this_fish',css_class='col-md-5',),
                
                Div('reserve_price',css_class='col-md-4',),
                Div('buy_now_price',css_class='col-md-4',),
                Div('part_of_auction',css_class='col-md-5',),
                Div('auction',css_class='col-md-8',),
                #HTML("<br><span class='text-danger col-xl-12'>You must select a pickup location before you can submit lots in an auction</span><br>"),
                Div('donation',css_class='col-md-4',),
                Div('run_duration',css_class='col-md-4',),
                Div('promoted',css_class='col-md-4',),
                Div('show_payment_pickup_info',css_class='col-md-12',),
                css_class='row',
            ),
            HTML("<span id='payment_pickup_info'><h4>Payment/pickup info</h4><br>"),
            Div(
                Div('local_pickup',css_class='col-md-6',),
                Div('shipping_locations',css_class='col-md-6',),
                Div('other_text',css_class='col-md-12',),
                Div('payment_cash',css_class='col-md-4',),
                Div('payment_paypal',css_class='col-md-4',),
                Div('payment_other',css_class='col-md-4',),
                Div('payment_other_method',css_class='col-md-4',),
                Div('payment_other_address',css_class='col-md-8',),
                css_class='row',
            ),
            HTML("</span>"),
            Submit('submit', "Save", css_class='create-update-lot btn-success mt-1 mr-1'),
            HTML("</span>"),
        )

    def clean(self):
        cleaned_data = super().clean()
        create_new_species = cleaned_data.get("create_new_species")
        new_species_name = cleaned_data.get("new_species_name")
        new_species_scientific_name = cleaned_data.get("new_species_scientific_name")
        new_species_category = cleaned_data.get("new_species_category")
        if create_new_species:
            if not new_species_name:
                self.add_error('new_species_name', "Enter the common name of the new species to create")
            if not new_species_scientific_name:
                self.add_error('new_species_scientific_name', "Enter the scientific name of the new species to create")
            if not new_species_category:
                self.add_error("new_species_category", "Pick a category")
        
        # this is now handled more seamlessly in LotValidation.form_valid -- the user can always edit it later
        #image = cleaned_data.get("image")
        #image_source = cleaned_data.get("image_source")
        #if image and not image_source:
        #    self.add_error('image_source', "Is this your picture?")
        
        # this doesn't really matter either - if the user screws with the client side validation, the lot simply won't be available
        auction = cleaned_data.get("auction")
        part_of_auction = cleaned_data.get("part_of_auction")
        if part_of_auction == "True":
            if auction is None:
                self.add_error('auction', "Select an auction")
        else:
            # set auction to empty
            self.cleaned_data['auction'] = None
            auction = None
            # fixme
            #if not self.user.is_superuser:
            #    self.add_error('part_of_auction', "This feature will be available starting in April 2021")
            # end fixme
            if not cleaned_data.get("shipping_locations") and not cleaned_data.get("local_pickup"):
                self.add_error('show_payment_pickup_info', "Select local pickup and/or a location to ship to")
            if not cleaned_data.get("payment_cash") and not cleaned_data.get("payment_paypal") and not cleaned_data.get("payment_other"):
                self.add_error('show_payment_pickup_info', "Select at least one payment method")
            if cleaned_data.get("payment_other") and not cleaned_data.get("payment_other_method"):
                self.add_error('payment_other_method', "Enter your payment method")

        if auction:
            try:
                ban = UserBan.objects.get(banned_user=self.user.pk, user=lot.auction.created_by.pk)
                self.add_error('auction', "The owner of this auction has banned you from submitting lots")
            except:
                pass
            #thisAuction = Auction.objects.get(pk=auction)
            if auction.max_lots_per_user:
                numberOfLots = Lot.objects.filter(user=self.user, auction=auction).count()
                if numberOfLots >= auction.max_lots_per_user:
                    self.add_error('auction', f"You can't add more lots to this auction (Limit: {auction.max_lots_per_user})")

            
        # check to see if this lot exists already
        try:
            existingLot = Lot.objects.filter(user=self.user, lot_name=cleaned_data.get("lot_name"), description=cleaned_data.get("description"), active = True).exclude(pk=self.instance.pk)
            if existingLot:
                self.add_error('description', "You've already added a lot exactly like this.  If you mean to submit another lot, change something here so it's unique")
        except:
            pass
        return self.cleaned_data

        
class CustomSignupForm(SignupForm):
    """To require firstname and lastname when signing up"""
    first_name = forms.CharField(max_length=30, label='First Name')
    last_name = forms.CharField(max_length=30, label='Last Name')
    def signup(self, request, user):
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.save()
        return user

class UserLocation(forms.ModelForm):
    """
    We need to have a form based on userdata in order to set the latitude and longitude correctly.
    But from a user's standpoint, it makes sense to set their name on the same form
    """
    first_name = forms.CharField(max_length=30, label='First name', required=True)
    last_name = forms.CharField(max_length=150, label='Last name', required=True)
    class Meta:
        model = UserData
        fields = (
            'phone_number',
            'location',
            'location_coordinates',
            'address',
            'club',
        )
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['address'].widget=forms.Textarea()
        self.fields['address'].widget.attrs = {'rows': 3}
        self.fields['address'].required = True
        self.fields['location'].help_text = "Optional. You'll be notified about new lots that can ship to this location."
        self.fields['phone_number'].help_text = "Optional"
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'user-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div('first_name',css_class='col-md-6',),
                Div('last_name',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('phone_number',css_class='col-md-4',),
                Div('location',css_class='col-md-4',),
                Div('club',css_class='col-md-4',),
                css_class='row',
            ),
            Div(
                Div('address'),
                Div('location_coordinates'),
            ),
            Submit('submit', 'Save', css_class='btn-success'),
        )

class ChangeUsernameForm(forms.ModelForm):
    """Needed to allow users to change their username"""
    class Meta:
        model = User
        fields = ('username', )
        exclude = ('last_login', 'is_superuser', 'groups',  'is_staff', 'is_active', 'date_joined', 'email', 'user_permissions',)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'user-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div('username',css_class='col-md-6',),
                css_class='row',
            ),
            Submit('submit', 'Save', css_class='btn-success'),
        )

class ChangeUserPreferencesForm(forms.ModelForm):
    class Meta:
        model = UserData
        fields = ('email_visible', 'use_dark_theme', 'use_list_view','email_me_about_new_auctions','email_me_about_new_auctions_distance',\
            'email_me_about_new_local_lots','local_distance', 'email_me_about_new_lots_ship_to_location', 'email_me_when_people_comment_on_my_lots',\
            )
        exclude = (
            'user','phone_number','address','location','location_coordinates',\
            'last_auction_used','last_activity','latitude','longitude',\
            'paypal_email_address','unsubscribe_link',\
            'has_unsubscribed','rank_unique_species','number_unique_species','rank_total_lots',\
            'number_total_lots','rank_total_spent','number_total_spent','rank_total_bids','number_total_bids',\
            'number_total_sold','rank_total_sold','total_volume',\
            'rank_volume','seller_percentile','buyer_percentile','volume_percentile','club',
        )
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_id = 'user-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div('email_visible',css_class='col-md-4',),
                Div('use_list_view',css_class='col-md-4',),
                Div('use_dark_theme',css_class='col-md-4',),
                css_class='row',
            ),
            HTML("<h4>Notifications</h4><br>"),
            Div(
                Div('email_me_when_people_comment_on_my_lots',css_class='col-md-6',),
                css_class='row',
            ),
            HTML("You'll get one email per week that contains an update on everything you've checked below<br><br>"),
            Div(
                Div('email_me_about_new_auctions',css_class='col-md-5',),
                Div('email_me_about_new_auctions_distance',css_class='col-md-3',),
                css_class='row',
            ),
            Div(
                Div('email_me_about_new_local_lots',css_class='col-md-5',),
                Div('local_distance',css_class='col-md-3',),
                css_class='row',
            ),
            Div(
                Div('email_me_about_new_lots_ship_to_location',css_class='col-md-12',),
                css_class='row',
            ),
            # Div(
            #     Div('location',css_class='col-md-6',),
            #     
            #     css_class='row',
            # ),
            Submit('submit', 'Save', css_class='btn-success'),
        )

    def clean(self):
        cleaned_data = super().clean()
        #image = cleaned_data.get("image")
        #image_source = cleaned_data.get("image_source")
        #if image and not image_source:
        #    self.add_error('image_source', "Is this your picture?")





