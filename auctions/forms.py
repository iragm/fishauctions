from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Submit
from crispy_forms.bootstrap import Div, Field
from django import forms
from .models import Lot, Bid, Auction
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
        
    def save(self, *args, **kwargs):
        kwargs['commit']=False
        obj = super().save(*args, **kwargs)
        # if self.req:
        #     obj.user = self.req.user.id
        # if self.lot:
        #     obj.lot_number = self.req.lot
        #print(str(obj.user)+ " has placed a bid on " + str(obj.lot_number))
        obj.save()
        return obj

    class Meta:
        model = Bid
        fields = [
            'user',
            'lot_number',
            'amount',
        ]

class CreateAuctionForm(forms.ModelForm):
    class Meta:
        model = Auction
        fields = ['title', 'notes', 'lot_entry_fee','unsold_lot_fee','winning_bid_percent_to_club', 'date_start', 'date_end', \
            'pickup_location', 'pickup_location_map', 'pickup_time', 'alternate_pickup_location', 'alternate_pickup_location_map',\
            'alternate_pickup_time', 'area']
        exclude = ['slug', 'sealed_bid', 'watch_warning_email_sent', 'invoiced', 'created_by', 'code_to_add_lots']
        widgets = {
            'date_start': DateTimePickerInput(),
            'date_end': DateTimePickerInput(),
            'pickup_time': DateTimePickerInput(),
            'alternate_pickup_time': DateTimePickerInput(),
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
            'area',
            'notes',
            Div(
                Div('lot_entry_fee',css_class='col-md-4',),
                Div('winning_bid_percent_to_club',css_class='col-md-4',),
                Div('unsold_lot_fee',css_class='col-md-4',),
                css_class='row',
            ),
            Div(
                Div('date_start',css_class='col-md-6',),
                Div('date_end',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('pickup_location',css_class='col-md-8',),
                Div('pickup_time',css_class='col-md-4',),
                css_class='row',
            ),
            'pickup_location_map',
            Div(
                Div('alternate_pickup_location',css_class='col-md-8',),
                Div('alternate_pickup_time',css_class='col-md-4',),
                css_class='row',
            ),
            'alternate_pickup_location_map',
            Submit('submit', 'Save', css_class='btn-success'),
        )

    def clean(self):
        cleaned_data = super().clean()
        #image = cleaned_data.get("image")
        #image_source = cleaned_data.get("image_source")
        #if image and not image_source:
        #    self.add_error('image_source', "Is this your picture?")

class CreateLotForm(forms.ModelForm):
    class Meta:
        model = Lot
        fields = ('lot_name','i_bred_this_fish','image','image_source','description','quantity','reserve_price','species_category','auction','donation')
        exclude = [ "user"]
        widgets = {
            'description': forms.Textarea,
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['description'].widget.attrs = {'rows': 3}
        # Default auction should be the most recent non-ended auction
        auctions = Auction.objects.all().filter(date_end__gte=timezone.now()).order_by('-date_end')
        try:
            self.fields['auction'].initial = auctions[0]
        except:
            # no non-ended auctions
            pass
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.form_id = 'lot-form'
        self.helper.form_class = ''
        self.helper.layout = Layout(
            'lot_name',
            'species_category',
            'image',
            'image_source',
            'description',
            'i_bred_this_fish',
            'donation',
            'quantity',
            'reserve_price',
            'auction',
            Submit('submit', "Save", css_class='btn-success'),
        )
    def clean(self):
        cleaned_data = super().clean()
        image = cleaned_data.get("image")
        image_source = cleaned_data.get("image_source")
        auction = cleaned_data.get("auction")
        if image and not image_source:
            self.add_error('image_source', "Is this your picture?")
        if not auction:
            self.add_error('auction', "Select an auction")