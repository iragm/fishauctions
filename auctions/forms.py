from allauth.account.forms import SignupForm
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Submit
from crispy_forms.bootstrap import Div, Field
from django import forms
from .models import Lot, Bid, Auction, User, UserData, Location, Club, PickupLocation, AuctionTOS
from django.forms import ModelForm, HiddenInput
from bootstrap_datepicker_plus import DateTimePickerInput
from django.utils import timezone

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
        self.helper.layout = Layout(
            'user',
            'lot_number',
            'amount',
            Submit('submit', 'Place bid', css_class='btn-success'),
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

class AuctionTOSForm(forms.ModelForm):
    def __init__(self, user, auction, *args, **kwargs):
        self.user = user
        self.auction = auction
        super().__init__(*args, **kwargs)
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_class = 'form-inline'
        self.helper.layout = Layout(
            'user',
            'auction',
            'pickup_location',
            Submit('submit', 'Confirm pickup location and view lots', css_class='btn-success'),
        )
        self.fields['pickup_location'].queryset = PickupLocation.objects.filter(auction=self.auction).order_by('name')
        self.fields['pickup_location'].label = "Yes, I will be at&nbsp;&nbsp;&nbsp;"
        self.fields['user'].widget = HiddenInput()
        self.fields['auction'].widget = HiddenInput()
        
    # def save(self, *args, **kwargs):
    #     kwargs['commit']=False
    #     obj = super().save(*args, **kwargs)
    #     obj.user = self.user
    #     obj.auction = self.auction
    #     obj.save()
    #     return obj

    class Meta:
        model = AuctionTOS
        fields = [
            'user',
            'auction',
            'pickup_location',
        ]

class PickupLocationForm(forms.ModelForm):
    class Meta:
        model = PickupLocation
        fields = ['name', 'auction', 'description', 'google_map_iframe', 'pickup_time', 'second_pickup_time']
        exclude = ['user']
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
        self.helper.form_class = ''
        self.helper.layout = Layout(
            'name',
            'description',
            'google_map_iframe',
            Div(
                Div('auction',css_class='col-md-4',),
                Div('pickup_time',css_class='col-md-4',),
                Div('second_pickup_time',css_class='col-md-4',),
                css_class='row',
            ),
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
            'lot_submission_end_date', 'location']
        exclude = ['slug', 'sealed_bid', 'watch_warning_email_sent', 'invoiced', 'created_by', 'code_to_add_lots', \
            'pickup_location', 'pickup_location_map', 'pickup_time', 'alternate_pickup_location', 'alternate_pickup_location_map',\
            'alternate_pickup_time',]
        widgets = {
            'date_start': DateTimePickerInput(),
            'date_end': DateTimePickerInput(),
            'lot_submission_end_date': DateTimePickerInput(),
            #'pickup_time': DateTimePickerInput(),
            #'alternate_pickup_time': DateTimePickerInput(),
            'notes': forms.Textarea,
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['notes'].widget.attrs = {'rows': 3}
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_id = 'auction-form'
        self.helper.form_class = ''
        self.helper.layout = Layout(
            'title',
            'location',
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
            Submit('submit', 'Save', css_class='btn-success'),
        )

    def clean(self):
        cleaned_data = super().clean()
        #image = cleaned_data.get("image")
        #image_source = cleaned_data.get("image_source")
        #if image and not image_source:
        #    self.add_error('image_source', "Is this your picture?")

class CreateLotForm(forms.ModelForm):
    """Form for creating or updating of lots"""
    # Fields not included in the Lot model
    # These are needed to add new species
    species_search = forms.CharField(max_length=200, required = False)
    species_search.help_text = "Search here for a latin or common name"
    create_new_species = forms.BooleanField(required = False)
    create_new_species.help_text = "Check if this species/item isn't available"
    new_species_name = forms.CharField(max_length=200, required = False)
    new_species_name.help_text = "Enter the common name of this species"
    new_species_scientific_name = forms.CharField(max_length=200, required = False)
    new_species_scientific_name.help_text = "Enter the Latin name of this species"
    
    class Meta:
        model = Lot
        fields = ('lot_name', 'species', 'species_search', 'create_new_species', 'new_species_name', 'new_species_scientific_name',\
            'i_bred_this_fish', 'image','image_source','description','quantity','reserve_price','species_category',\
            'auction','donation')
        exclude = [ "user"]
        widgets = {
            'description': forms.Textarea,
            'species': forms.HiddenInput,
        }
    def __init__(self, *args, **kwargs):
        try:
            self.user = kwargs.pop('user')
        except:
            pass
        super().__init__(*args, **kwargs)
        self.fields['description'].widget.attrs = {'rows': 3}
        self.fields['species_category'].required = True
        self.fields['auction'].queryset = Auction.objects.filter(lot_submission_end_date__gte=timezone.now()).filter(date_start__lte=timezone.now()).order_by('date_end')
        # Default auction selection:
        try:
            auctions = Auction.objects.filter(lot_submission_end_date__gte=timezone.now()).filter(date_start__lte=timezone.now()).order_by('date_end')
        #    self.fields['auction'].initial = auctions[0] # this would set a default value.  We should make users pick this manually so they don't accidentally submit to the wrong auction
        except:
            # no non-ended auctions
            pass
        try:
            # see if this user's last auction is still available
            obj, created = UserData.objects.get_or_create(user=self.user)
            lastUserAuction = obj.last_auction_used
            if lastUserAuction.lot_submission_end_date > timezone.now():
                print(lastUserAuction)
                #self.fields['auction'].initial = lastUserAuction
        except:
            pass
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_id = 'lot-form'
        self.helper.form_class = ''
        self.helper.layout = Layout(
            'species_search',
            'lot_name',
            'species',
            'species_category',
            Div(
                Div('i_bred_this_fish',css_class='col-md-6',),
                Div('create_new_species',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('new_species_name',css_class='col-md-6',),
                Div('new_species_scientific_name',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('image',css_class='col-md-8',),
                Div('image_source',css_class='col-md-4',),
                css_class='row',
            ),
            'description',            
            
            'quantity',
            Div(
                Div('reserve_price',css_class='col-md-8',),
                Div('donation',css_class='col-md-4',),
                css_class='row',
            ),
            'auction',
            Submit('submit', "Save", css_class='btn-success'),
        )
    def clean(self):
        cleaned_data = super().clean()
        
        create_new_species = cleaned_data.get("create_new_species")
        new_species_name = cleaned_data.get("new_species_name")
        new_species_scientific_name = cleaned_data.get("new_species_scientific_name")
        if create_new_species:
            if not new_species_name:
                self.add_error('new_species_name', "Enter the common name of the new species to create")
            if not new_species_scientific_name:
                self.add_error('new_species_scientific_name', "Enter the scientific name of the new species to create")
        
        image = cleaned_data.get("image")
        image_source = cleaned_data.get("image_source")
        if image and not image_source:
            self.add_error('image_source', "Is this your picture?")
        
        auction = cleaned_data.get("auction")
        if not auction:
            self.add_error('auction', "Select an auction")
        
        # check to see if this lot exists already
        try:
            existingLot = Lot.objects.filter(user=self.user, lot_name=cleaned_data.get("lot_name"), description=cleaned_data.get("description"), active = True)
            if existingLot:
                self.add_error('description', "You've already added a lot exactly like this.  If you mean to submit another lot, change something here so it's unique")
        except:
            pass
        
class CustomSignupForm(SignupForm):
    """To require firstname and lastname when signing up"""
    first_name = forms.CharField(max_length=30, label='First Name')
    last_name = forms.CharField(max_length=30, label='Last Name')
    def signup(self, request, user):
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.save()
        return user

class UpdateUserForm(forms.ModelForm):
    phone_number = forms.CharField(max_length=30, label='Cell phone number', required=False)
    address = forms.CharField(max_length=255, help_text="Your complete mailing address.  If you sell lots, we'll mail your check here.", required=False, widget=forms.Textarea())
    location = forms.ModelChoiceField(queryset=Location.objects.filter(), required=False)
    club = forms.ModelChoiceField(queryset=Club.objects.filter(), required=False)
    email_visible = forms.BooleanField(required=False, help_text = "Show your email address on your user page.  This will be visible only to logged in users.")
    use_list_view = forms.BooleanField(required=False, help_text = "When viewing lots, show them as a list instead of as tiles.")

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name','phone_number', 'address', 'location', 'email_visible', 'use_list_view')
        exclude = ('last_login', 'is_superuser', 'groups', 'user_permissions', 'is_staff', 'is_active', 'date_joined', 'email',)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['address'].widget.attrs = {'rows': 3}
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_id = 'user-form'
        self.helper.form_class = ''
        self.helper.layout = Layout(
            Div(
                Div('first_name',css_class='col-md-6',),
                Div('last_name',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('username',css_class='col-md-6',),
                Div('phone_number',css_class='col-md-6',),
                css_class='row',
            ),
            'address',
            Div(
                Div('location',css_class='col-md-6',),
                Div('club',css_class='col-md-6',),
                css_class='row',
            ),
            'email_visible',
            'use_list_view',
            Submit('submit', 'Save', css_class='btn-success'),
        )

    def clean(self):
        cleaned_data = super().clean()
        #image = cleaned_data.get("image")
        #image_source = cleaned_data.get("image_source")
        #if image and not image_source:
        #    self.add_error('image_source', "Is this your picture?")