from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Submit
from django import forms
from .models import Lot, Bid
from django.forms import ModelForm, HiddenInput
#from django.core.exceptions import ValidationError


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

class CreateLotForm(forms.ModelForm):
    class Meta:
        model = Lot
        fields = ('lot_name','i_bred_this_fish','image','image_source','description','quantity','reserve_price','category','auction',)
        exclude = [ "user"]
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            'lot_name',
            'category',
            'image',
            'image_source',
            'description',
            'i_bred_this_fish',
            'quantity',
            'reserve_price',
            #'auction',
            Submit('submit', 'Create lot', css_class='btn-success'),
        )
    def clean(self):
        cleaned_data = super().clean()
        image = cleaned_data.get("image")
        image_source = cleaned_data.get("image_source")
        if image and not image_source:
            self.add_error('image_source', "Is this your picture?")