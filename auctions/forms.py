from allauth.account.forms import SignupForm
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Submit, HTML
from crispy_forms.bootstrap import Div, Field, PrependedAppendedText
from django import forms
from .models import Lot, Bid, Auction, User, UserData, Location, Club, PickupLocation, AuctionTOS, Invoice, Category, LotImage, UserBan, UserLabelPrefs
from django.forms import ModelForm, HiddenInput, RadioSelect, ModelChoiceField
# from bootstrap_datepicker_plus import DateTimePickerInput
from bootstrap_datepicker_plus.widgets import DateTimePickerInput # https://github.com/monim67/django-bootstrap-datepicker-plus/issues/66
from django.utils import timezone
from location_field.models.plain import PlainLocationField
#from django.core.exceptions import ValidationError
import datetime
import pytz
from django.core.validators import MinValueValidator
from dal import autocomplete
from django.urls import reverse
from django.forms import BaseModelFormSet
from django.template.loader import render_to_string

# class DateInput(forms.DateInput):
#     input_type = 'datetime-local'

class QuickAddTOS(forms.ModelForm):
    """Add a new user to an auction by filling out only the most important fields"""
    
    class Meta:
        model = AuctionTOS
        fields = [
            'bidder_number',
            'name',
            'email',
            'phone_number',
            'address',
            'pickup_location',
        ]
        widgets = {
            'address': forms.Textarea(attrs={'rows':2}),
        }
    def __init__(self, *args, **kwargs):
        self.auction = kwargs.pop('auction')
        self.bidder_numbers_on_this_form = kwargs.pop('bidder_numbers_on_this_form')
        super().__init__(*args, **kwargs)
        self.fields['bidder_number'].help_text = ""
        self.fields['name'].help_text = ""
        self.fields['email'].help_text = ""
        self.fields['phone_number'].help_text = ""
        self.fields['address'].help_text = ""
        self.fields['pickup_location'].queryset = self.auction.location_qs
        if not self.auction.multi_location:
            self.fields['pickup_location'].initial = self.auction.location_qs.first()

    def clean(self):
        cleaned_data = super().clean()
        bidder_number = cleaned_data.get("bidder_number")
        if bidder_number:
            existing_tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction)
            pk = cleaned_data.get("pk")
            if pk:
                existing_tos = existing_tos.exclude(pk=pk)
            else:
                self.bidder_numbers_on_this_form.append(bidder_number)
            if existing_tos.count() or self.bidder_numbers_on_this_form.count(bidder_number) > 1:
                self.add_error('bidder_number', "This bidder number is already in use")
        if cleaned_data.get("email") and not cleaned_data.get("pk"):
            # duplicate email check for new users only
            existing_tos = AuctionTOS.objects.filter(email=cleaned_data.get("email"), auction=self.auction).first()
            if existing_tos:
                self.add_error('email', "This email address is already in use")
        name = cleaned_data.get("name")
        if not name:
            self.add_error('name', "Name is required")
        # # duplicate name check for new users only
        # else:
        #     if not cleaned_data.get('pk'):
        #         existing_tos = AuctionTOS.objects.filter(name=name, auction=self.auction).first()
        #         if existing_tos:
        #             self.add_error('name', "This name is already in use, add a middle name or a number or something to make it unique")
        return cleaned_data

class QuickAddLot(forms.ModelForm):
    """Add a new lot by filling out only the most important fields"""
    
    class Meta:
        model = Lot
        fields = [
            'custom_lot_number',
            'lot_name',
            'species_category',
            'i_bred_this_fish',
            'quantity',
            'donation',
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows':2}),
        }
    def __init__(self, *args, **kwargs):
        self.auction = kwargs.pop('auction')
        self.custom_lot_numbers_used = kwargs.pop('custom_lot_numbers_used')
        self.is_admin = kwargs.pop('is_admin')
        self.tos = kwargs.pop('tos')
        self.lot_count = 0
        # we need to work around the case where a user enters duplicate custom lot numbers
        super().__init__(*args, **kwargs)
        self.fields['custom_lot_number'].label = "Custom lot number"
        self.fields['custom_lot_number'].help_text = ""
        self.fields['lot_name'].label = "Lot name"
        self.fields['lot_name'].help_text = ""
        if not self.is_admin:
            self.fields['custom_lot_number'].widget = HiddenInput()
        #if not self.auction.use_categories:
        if True: # hide category field, it's automatically set now
            self.fields['species_category'].widget = HiddenInput()
        self.fields['species_category'].label = "Category"
        self.fields['species_category'].help_text = ""
        self.fields['i_bred_this_fish'].label = "Breeder points"
        self.fields['i_bred_this_fish'].help_text = ""
        self.fields['quantity'].help_text = ""
        self.fields['donation'].help_text = ""
        self.fields['species_category'].initial = Category.objects.get(pk=21) # uncategorized
        self.fields['quantity'].initial = 1


    def clean(self):
        cleaned_data = super().clean()
        custom_lot_number = cleaned_data.get("custom_lot_number")
        if custom_lot_number:
            existing_lots = Lot.objects.exclude(is_deleted=True).filter(custom_lot_number=custom_lot_number, auction=self.auction)
            lot_number = cleaned_data.get("lot_number")
            if lot_number:
                existing_lots = existing_lots.exclude(lot_number=lot_number.pk)
            else:
                self.custom_lot_numbers_used.append(custom_lot_number)
            if existing_lots.count() or self.custom_lot_numbers_used.count(custom_lot_number) > 1:
                self.add_error('custom_lot_number', "This lot number is already in use")
        # we need to make sure users can't add extra lots
        if not self.is_admin and self.auction.max_lots_per_user:
            existing_lots = self.tos.unbanned_lot_qs
            if self.auction.allow_additional_lots_as_donation:
                existing_lots = existing_lots.exclude(donation=True)
            if not cleaned_data.get("lot_number"):
                # new lots only
                total_lots = existing_lots.count() + self.lot_count
                if total_lots > self.auction.max_lots_per_user:
                    if self.auction.allow_additional_lots_as_donation:
                        if not cleaned_data.get("donation"):
                            self.add_error('donation', "Any additional lots need to be a donation")
                    else:
                        self.add_error('lot_name', "You can't add more lots to this auction")
                # increment counter of unsaved lots
                if self.auction.allow_additional_lots_as_donation:
                    if not cleaned_data.get("donation"):
                        self.lot_count += 1
                else:
                    self.lot_count += 1
        return cleaned_data
    
    # def get_form_kwargs(self):
    #     kwargs = super().get_form_kwargs()
    #     kwargs['pk'] = self.kwargs['pk']
    #     print(kwargs['auction'])
    #     return kwargs

# class BaseLotFormSet(BaseModelFormSet):
#     """This is not used anywhere, see EditLot instead
#     Validation for QuickAddLot
#     fixme - we need to pass auction here as a kwarg"""
#     def __init__(self, *args, **kwargs):
#         #self.auction = kwargs.pop('auction')
#         #print(self.auction)
#         super().__init__(*args, **kwargs)
#         self.queryset = Lot.objects.filter(auctiontos_seller__bidder_number="259")
#         return


#     def clean(self):
#         if any(self.errors):
#             return
#         issues = False
#         for form in self.forms:
#             if form.cleaned_data:
#                 custom_lot_number = form.cleaned_data['custom_lot_number']
#                 if custom_lot_number:
#                     other_lots = Lot.objects.exclude(is_deleted=True).filter(auction=self.auction, custom_lot_number=custom_lot_number).count()
#                     #if other_lots > 1:
#                     form.add_error('custom_lot_number', "Lot number already in use")
#                     issues = True

#                 if issues:
#                     raise forms.ValidationError(
#                         'One or more lot numbers are already in use',
#                         code='lot_numbers'
#                     )
class TOSFormSetHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        #self.auction = kwargs['auction']
        super().__init__(*args, **kwargs)
        self.form_method = 'post'
        

        # self.layout = Layout(
        #     Div(
        #         Div('custom_lot_number',css_class='col-sm-5',),
        #         Div('lot_name',css_class='col-sm-7',),
        #         css_class='row',
        #     ),
        #     Div(
        #         Div('quantity',css_class='col-sm-4',),
        #         Div('donation',css_class='col-sm-4',),
        #         Div('i_bred_this_fish',css_class='col-sm-4',),
        #         css_class='row',
        #     ),
        # )
        #self.add_input(Submit('submit', 'Save'))
        self.template = 'auctions/bulk_add_users_row.html'

class LotFormSetHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        #self.auction = kwargs['auction']
        super().__init__(*args, **kwargs)
        self.form_method = 'post'
        

        # self.layout = Layout(
        #     Div(
        #         Div('custom_lot_number',css_class='col-sm-5',),
        #         Div('lot_name',css_class='col-sm-7',),
        #         css_class='row',
        #     ),
        #     Div(
        #         Div('quantity',css_class='col-sm-4',),
        #         Div('donation',css_class='col-sm-4',),
        #         Div('i_bred_this_fish',css_class='col-sm-4',),
        #         css_class='row',
        #     ),
        # )
        #self.add_input(Submit('submit', 'Save'))
        self.template = 'auctions/bulk_add_lots_row.html'

class WinnerLot(forms.Form):
    """Used to quickly set the winners on lots.  Note that this does not use forms.ModelForm"""
    # note the use of CharFields here; if we use ChoiceFields instead, we get validation errors on submit
    
    lot = forms.CharField(
        widget=autocomplete.Select2(
        url='lot-autocomplete',
        forward=['auction'],
        attrs={'data-html': True,
        'data-container-css-class': '',
        })
        )
    winner = forms.CharField(
        widget=autocomplete.Select2(
        url='auctiontos-autocomplete',
        forward=['auction','invoice'],
        attrs={'data-html': True,
        'data-container-css-class': '',
        })
        )
    winning_price = forms.IntegerField(label="Sell price", min_value=0)
    invoice = forms.CharField(label='Invoice', max_length=100)
    auction = forms.CharField(label='Auction', max_length=100)

    def __init__(self, auction, *args, **kwargs):
        self.auction_pk = auction.pk
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_class = 'form'
        self.helper.form_id = 'lot-form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'invoice',
            'auction',
            'lot',
            PrependedAppendedText('winning_price', '$', '.00' ),
            'winner',
            # Div(
            #     Div('lot',css_class='col-md-5',),
            #     Div('winner',css_class='col-md-3',),
            #     Div('winning_price',css_class='col-md-3',),
            #     css_class='row',
            # ),
            Div(
                HTML(f'<button type="submit" class="btn btn-success float-right">Save</button>'),
                css_class='row',
            )
        )
        self.fields['auction'].initial = self.auction_pk
        self.fields['auction'].widget = HiddenInput()
        self.fields['invoice'].widget = HiddenInput()
        self.fields['invoice'].initial = "True"

    class Meta:
        fields = [
            'auction',
            'lot',
            'winner',
            'winning_price',
        ]

class WinnerLotSimple(WinnerLot):
    """Simplified form using char fields instead of autocomplete fields"""
    lot = forms.CharField(max_length=20)
    winner = forms.CharField(max_length=20)

class DeleteAuctionTOS(forms.Form):
    """For deleting auctionTOS and optionally merging lots, admins only"""
    delete_lots = forms.BooleanField(required = False)
    merge_with = forms.CharField(
        widget=autocomplete.Select2(
        url='auctiontos-autocomplete',
        forward=['auction'],
        attrs={'data-html': True,
        'data-container-css-class': '',
        })
        )
    auction = forms.CharField(label='Auction', max_length=100)
    def __init__(self, auctiontos, auction, *args, **kwargs):
        self.auction = auction
        self.auctiontos = auctiontos
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_class = 'form'
        self.helper.form_id = 'auctiontos-delete-form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'auction',
            Div(
                'delete_lots',
                css_class='row',
            ),
            Div(
                Div('merge_with',css_class='col-sm-12',),
                css_class='row',
            css_id="merge_selection",
            ),
            Div(
                HTML('<a class="btn btn-secondary" href="javascript:window.history.back();">Keep this user</a>'),
                HTML(f'<button type="submit" class="btn btn-warning float-right">Delete</button>'),
                css_class="modal-footer",
            )
        )
        self.fields['auction'].widget = HiddenInput()
        self.fields['auction'].initial = self.auctiontos.auction.pk
        self.fields['merge_with'].required=False
        existing_lots = self.auctiontos.unbanned_lot_count
        bought_lots = self.auctiontos.bought_lots_qs.count()
        if not existing_lots and not bought_lots:
            self.fields['delete_lots'].widget = HiddenInput()
        else:
            self.fields['delete_lots'].label = f"Also delete {existing_lots} lot(s) for this user and mark {bought_lots} lot(s) that this user won as unsold"
            self.fields['delete_lots'].help_text = "Uncheck if this is a duplicate user.  Lot numbers will not be changed."
            self.fields['merge_with'].label = "To keep these lots, select a user to assign them to"

    def clean(self):
        cleaned_data = super().clean()
        delete_lots = cleaned_data.get("delete_lots")
        if not delete_lots:
            merge_with = cleaned_data.get("merge_with")
            if not merge_with:
                self.add_error('merge_with', "Select a new user to preserve this user's data")
            else:
                if AuctionTOS.objects.get(pk=merge_with).auction.pk != self.auctiontos.auction.pk:
                    self.add_error('merge_with', "This shouldn't even be possible!")
                if AuctionTOS.objects.get(pk=merge_with) == self.auctiontos:
                    self.add_error('merge_with', "You can't select the user you're about to delete")
        return cleaned_data


class EditLot(forms.ModelForm):
    """Used for HTMX calls to update Lot.
    For auction admins only.
    Note that unlike AuctionTOS (which has a similar form), this form will ONLY update lots, not create them"""
    def __init__(self, user, lot, auction, *args, **kwargs):
        self.user = user
        self.auction = auction
        self.lot = lot
        super().__init__(*args, **kwargs)
        post_url = reverse("auctionlotadmin", kwargs={'pk': lot.pk})
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_class = 'form'
        self.helper.form_id = 'lot-form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'auction',
            Div(
                Div('custom_lot_number',css_class='col-sm-5',),
                Div('banned',css_class='col-sm-7',),
                css_class='row',
            ),
            'lot_name',
            'species_category',
            'description',
            #'auctiontos_seller',
            Div(
                Div('quantity',css_class='col-sm-4',),
                Div('donation',css_class='col-sm-4',),
                Div('i_bred_this_fish',css_class='col-sm-4',),
                css_class='row',
            ),
            Div(
                Div('auctiontos_winner',css_class='col-sm-8',),
                Div('winning_price',css_class='col-sm-4',),
                css_class='row',
            ),
            Div(
                HTML('<button type="button" class="btn btn-secondary float-left" onclick="closeModal()">Cancel</button>'),
                HTML(f'<button hx-post="{post_url}" hx-target="#modals-here" type="submit" class="btn btn-success float-right">Save</button>'),
                css_class="modal-footer",
            )
        )
        #self.fields['species_category'].queryset = auction.location_qs #PickupLocation.objects.filter(auction=self.auction).order_by('name')
        self.fields['custom_lot_number'].initial = self.lot.custom_lot_number
        self.fields['auction'].initial = self.lot.auction
        self.fields['custom_lot_number'].help_text = "Leave blank to automatically generate"
        self.fields['lot_name'].initial = self.lot.lot_name
        self.fields['description'].initial = self.lot.lot_name
        #self.fields['auctiontos_seller'].initial = self.lot.auctiontos_seller
        self.fields['quantity'].initial = self.lot.quantity
        self.fields['donation'].initial = self.lot.donation
        self.fields['winning_price'].initial = self.lot.winning_price
        self.fields['species_category'].initial = self.lot.species_category
        self.fields['i_bred_this_fish'].initial = self.lot.i_bred_this_fish
        self.fields['banned'].initial = self.lot.banned
        self.fields['auctiontos_winner'].initial = self.lot.auctiontos_winner
        # and some housekeeping on labels and help text
        self.fields['winning_price'].label = "Price"
        self.fields['winning_price'].help_text = ""
        self.fields['lot_name'].help_text = ""
        self.fields['species_category'].help_text = ""
        self.fields['auctiontos_winner'].label = "Winner"
        winner_help_test = ""
        if lot.high_bidder:
            winner_help_test = f"High bidder: <span class='text-warning'>{lot.high_bidder}</span> Bid: <span class='text-warning'>${lot.high_bid}</span>"
        self.fields['auctiontos_winner'].help_text = winner_help_test
        #self.fields['auctiontos_seller'].label = "Seller"
        #self.fields['auctiontos_seller'].help_text = ""
        self.fields['quantity'].help_text = ""
        self.fields['donation'].help_text = ""
        self.fields['i_bred_this_fish'].label = "Breeder points"
        self.fields['i_bred_this_fish'].help_text = ""
        
        # auctiontos_autocomplete_url = reverse("auctiontos-autocomplete", kwargs={'slug': self.auction.slug})
        # self.fields['auctiontos_winner'].widget = autocomplete.ModelSelect2(url=auctiontos_autocomplete_url)
        # self.fields['auctiontos_winner'].widget.choices = self.fields['auctiontos_winner'].choices
        # #attrs = self.fields['auctiontos_winner'].widget.attrs
        #subattrs = attrs.setdefault('settings_overrides', {})
        #subattrs['url'] = "new/"
        #self.fields['auctiontos_seller'].widget = autocomplete.ModelSelect2(url='/better/') #.attrs.update(url=auctiontos_autocomplete_url)
        #self.fields['auctiontos_winner'].widget.attrs.update(url=auctiontos_autocomplete_url)

        # self.fields['auctiontos_winner'].widget = autocomplete.ModelSelect2(url=auctiontos_autocomplete_url, attrs={
        #     'forward': self.auction.slug,
        # });

    class Meta:
        model = Lot
        fields = [
            'lot_name',
            'custom_lot_number',
            'auction',
            'species_category',
            'description',
            #'auctiontos_seller',
            'quantity',
            'donation',
            'i_bred_this_fish',
            'banned',
            'auctiontos_winner',
            'winning_price'
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows':2}),
            #'auctiontos_seller': autocomplete.ModelSelect2(url='auctiontos-autocomplete', forward=['auction'], attrs={'data-html': True, 'data-container-css-class': ''}),
            'auctiontos_winner': autocomplete.ModelSelect2(url='auctiontos-autocomplete', forward=['auction'], attrs={'data-html': True, 'data-container-css-class': ''}),
            'auction': HiddenInput()
        }

    def clean(self):
        cleaned_data = super().clean()
        auction = cleaned_data.get("auction")
        if auction:
            if not auction.permission_check(self.user):
                self.add_error('auction', "How did you even manage to change this field?")
        custom_lot_number = cleaned_data.get("custom_lot_number")
        if custom_lot_number:
            other_lots = Lot.objects.exclude(is_deleted=True).filter(auction=auction, custom_lot_number=custom_lot_number).exclude(pk=self.lot.pk).count()
            if other_lots:
                self.add_error('custom_lot_number', "Lot number already in use")
        if not cleaned_data.get("auctiontos_winner") and cleaned_data.get("winning_price"):
            self.add_error('auctiontos_winner', "You need to set a winner")
        if cleaned_data.get("auctiontos_winner") and not cleaned_data.get("winning_price"):
            self.add_error('winning_price', "You need to set a sell price")
        return cleaned_data

class CreateEditAuctionTOS(forms.ModelForm):
    """Used for HTMX calls to update AuctionTOS.  For auction admins only."""
    def __init__(self, is_edit_form, auctiontos, auction, *args, **kwargs):
        self.is_edit_form = is_edit_form
        self.auction = auction
        self.auctiontos = auctiontos
        super().__init__(*args, **kwargs)
        delete_button_html = ""
        if self.is_edit_form:
            post_url = f'/api/auctiontos/{self.auctiontos.pk}/'
            delete_url = reverse("auctiontosdelete", kwargs={'pk': self.auctiontos.pk})
            delete_button_html = f"<a href={delete_url} class='btn btn-warning'>Delete</a>"
        else:
            post_url = f'/api/auctiontos/{self.auction.slug}/'
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_class = 'form'
        self.helper.form_id = 'user-form'
        self.helper.form_tag = True        
        self.helper.layout = Layout(
			'bidder_number',
            'name',
			'email',
			'phone_number',
			'address',
            'pickup_location',
            'selling_allowed',
            'is_admin',
            Div(
                HTML(f'{delete_button_html}<button type="button" class="btn btn-secondary float-left" onclick="closeModal()">Cancel</button>'),
                HTML(f'<button hx-post="{post_url}" hx-target="#modals-here" type="submit" class="btn btn-success float-right">Save</button>'),
                css_class="modal-footer",
            )
        )
        self.fields['name'].required=True
        self.fields['pickup_location'].queryset = auction.location_qs #PickupLocation.objects.filter(auction=self.auction).order_by('name')
        if self.is_edit_form:
            # hide fields if editing
            self.fields['bidder_number'].initial = self.auctiontos.bidder_number
            if self.auctiontos.unbanned_lot_count:
                self.fields['bidder_number'].help_text = f"<span class=''>This user has already added {self.auctiontos.unbanned_lot_count} lots.</span> Changing this will not update lot numbers, but invoices will still be accurate"
            self.fields['name'].initial = self.auctiontos.name
            self.fields['email'].initial = self.auctiontos.email
            try:
                self.fields['phone_number'].initial = self.auctiontos.phone_as_string
            except:
                self.fields['phone_number'].initial = self.auctiontos.phone_number
            self.fields['address'].initial = self.auctiontos.address
            self.fields['pickup_location'].initial = self.auctiontos.pickup_location.pk
            self.fields['is_admin'].initial = self.auctiontos.is_admin
            self.fields['selling_allowed'].initial = self.auctiontos.selling_allowed
        else:
            # special rule: default to the default location
            self.fields['is_admin'].widget = HiddenInput()
            if auction.location_qs.count() == 1:
                self.fields['pickup_location'].initial = auction.location_qs.first()
        if not auction.only_approved_sellers:
            self.fields['selling_allowed'].widget = HiddenInput()
    class Meta:
        model = AuctionTOS
        fields = [
            'bidder_number',
            'pickup_location',
            'is_admin',
			'name',
			'email',
			'phone_number',
			'address',
            'selling_allowed',
        ]
        widgets = {
            'address': forms.Textarea(attrs={'rows':3})
        }

    def clean(self):
        cleaned_data = super().clean()
        auction = cleaned_data.get("auction")
        if auction:
            if not auction.permission_check(self.user):
                self.add_error('auction', "How did you even manage to change this field?")
        bidder_number = cleaned_data.get("bidder_number")
        other_bidder_numbers = AuctionTOS.objects.filter(auction=self.auction, bidder_number=bidder_number)
        if self.auctiontos:
            other_bidder_numbers = other_bidder_numbers.exclude(pk=self.auctiontos.pk)
        if other_bidder_numbers.count():
            self.add_error('bidder_number', "Bidder number already in use")
        return cleaned_data

    # def clean(self):
    #     cleaned_data = super().clean()
    #     custom_lot_number = cleaned_data.get("custom_lot_number")
    #     if custom_lot_number:
    #         existing_lots = Lot.objects.exclude(is_deleted=True).filter(custom_lot_number=custom_lot_number, auction=self.auction)
    #         lot_number = cleaned_data.get("lot_number")
    #         if lot_number:
    #             existing_lots = existing_lots.exclude(lot_number=lot_number.pk)
    #         else:
    #             self.custom_lot_numbers_used.append(custom_lot_number)
    #         if existing_lots.count() or self.custom_lot_numbers_used.count(custom_lot_number) > 1:
    #             self.add_error('custom_lot_number', "This lot number is already in use")
    #     return cleaned_data

class CreateBid(forms.ModelForm):
    #amount = forms.IntegerField()
    def __init__(self, *args, **kwargs):
        self.req = kwargs.pop('request', None)
        self.lot = kwargs.pop('lot', None)
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_class = 'form-inline'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'user',
            'lot_number',
            'amount',
            Submit('submit', 'Place bid', css_class='place-bid btn-info'),
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
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_class = 'form'
        self.helper.form_id = 'invoice-form'
        self.helper.form_tag = True   
        self.helper.layout = Layout(
            'memo',
            HTML("<h5>Adjust</h5>"),
            Div(
            Div('adjustment_direction',css_class='col-lg-3',),
            PrependedAppendedText('adjustment', '$', '.00',wrapper_class='col-lg-3', ),
            Div('adjustment_notes',css_class='col-lg-6',),
            css_class='row',
            ),
            Submit('submit', 'Save', css_class='btn-success'), 
        )
        self.fields['adjustment_direction'].label = ""
        self.fields['adjustment'].label = ""
        self.fields['adjustment_notes'].label = ""
        self.fields['adjustment_notes'].help_text = f"Adjustment reason will be visible to the user"
        
    class Meta:
        model = Invoice
        fields = [
            'adjustment_direction',
            'adjustment',
            'adjustment_notes',
            'memo',
        ]

class AuctionJoin(forms.ModelForm):
    i_agree = forms.BooleanField(required = True)

    def __init__(self, user, auction, *args, **kwargs):
        self.user = user
        self.auction = auction
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_class = 'form-inline'
        self.helper.form_id = 'rule-form'
        self.helper.form_tag = True        
        self.helper.layout = Layout(
            'i_agree',
            'time_spent_reading_rules',
            'pickup_location',
            Submit('submit', 'Confirm pickup location', css_class='agree_tos btn-success'),
        )
        self.fields['pickup_location'].queryset = auction.location_qs #PickupLocation.objects.filter(auction=self.auction).order_by('name')
        self.fields['time_spent_reading_rules'].widget = HiddenInput()
        if self.auction.multi_location:
            self.fields['i_agree'].initial = True
            self.fields['i_agree'].widget = HiddenInput()
            self.fields['pickup_location'].label = "Yes, I will be at "
        else:
            # single location auction
            self.fields['pickup_location'].widget = HiddenInput()
            if self.auction.location_qs.count() == 1: # note: number_of_locations only gives you non-default locations
                self.fields['pickup_location'].initial = auction.location_qs[0]
                self.fields['i_agree'].label = f"Yes, I will be at {auction.location_qs[0]}"

    class Meta:
        model = AuctionTOS
        fields = [
            'i_agree', 'pickup_location', 'time_spent_reading_rules',
        ]

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
            'auction': forms.HiddenInput,
        }
    def __init__(self, user, auction, *args, **kwargs):
        timezone.activate(kwargs.pop('user_timezone'))
        self.is_edit_form = kwargs.pop('is_edit_form')
        self.pickup_location = kwargs.pop('pickup_location')
        super().__init__(*args, **kwargs)
        self.user = user
        self.auction = auction
        self.fields['description'].widget.attrs = {'rows': 3}
        if not self.auction.multi_location:
            # to keep things simple when creating a new auction with only one location
            self.fields['second_pickup_time'].widget=forms.HiddenInput()
            self.fields['description'].widget=forms.HiddenInput()
            self.fields['pickup_location_contact_name'].widget=forms.HiddenInput()
            self.fields['pickup_location_contact_phone'].widget=forms.HiddenInput()
            self.fields['pickup_location_contact_email'].widget=forms.HiddenInput()
            self.fields['users_must_coordinate_pickup'].widget=forms.HiddenInput()
        # if self.user.is_superuser:
        #     self.fields['auction'].queryset = Auction.objects.filter(date_end__gte=timezone.now()).order_by('date_end')
        # else:
        #     self.fields['auction'].queryset = Auction.objects.filter(created_by=self.user).filter(date_end__gte=timezone.now()).order_by('date_end')
        delete_button_html = ""
        if self.is_edit_form:
            delete_button_html = f"<a href='{reverse('delete_pickup', kwargs={'pk': self.pickup_location.pk})}' class='btn btn-warning mr-2'>Delete this location</a>"
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'location-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.fields['auction'].initial = auction
        self.helper.layout = Layout(
            'name',
            'description',
            'auction',
            #HTML("<h4>Contact info</h4>"),
            Div(
                Div('pickup_location_contact_name',css_class='col-md-6',),
                Div('pickup_location_contact_phone',css_class='col-md-6',),
                Div('pickup_location_contact_email',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('pickup_time',css_class='col-md-4',),
                Div('second_pickup_time',css_class='col-md-4',),
                Div('users_must_coordinate_pickup',css_class='col-md-4',),
                css_class='row',
            ),
            'address',
            'location_coordinates',
            Div(
                HTML("The pin on the map must be at the <span class='text-warning'>exact location of the pickup location!</span><br><small>People will get directions based on this pin, and will get lost if it's not in the right place</small>"),
            ),
            HTML(f'<a class="btn btn-secondary mr-2" href="javascript:window.history.back();">Cancel</a>{delete_button_html}'),
            Submit('submit', 'Save', css_class='btn-success'),
        )
    
    def clean(self):
        cleaned_data = super().clean()
        auction = cleaned_data.get("auction")
        if auction:
            if not auction.permission_check(self.user):
                self.add_error('auction', "You can only add pickup locations to your own auctions")
        return cleaned_data

class CreateImageForm(forms.ModelForm):
    class Meta:
        model = LotImage
        fields = ['image', 'image_source', 'caption',]
        exclude = ['is_primary', 'lot_number',]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'auction-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'image',
            Div(
                Div('image_source',css_class='col-md-4',),
                Div('caption',css_class='col-md-8',),
                css_class='row',
            ),
            Submit('submit', 'Save', css_class='create-update-image btn-success'),
        )

class CreateAuctionForm(forms.ModelForm):
    """Create a new an auction"""
    
    is_online = forms.BooleanField(required=False, widget=forms.HiddenInput())
    cloned_from = forms.CharField(required=False, widget=forms.HiddenInput())
    class Meta:
        model = Auction
        fields = ['date_start', 'title', ]
        widgets = {
            'date_start': DateTimePickerInput(),
            #'is_online': HiddenInput(),
        }
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user')
        self.auction = None # this will be the instance of Auction to clone from
        self.cloned_from = kwargs.pop('cloned_from') # slug only at this point
        timezone.activate(kwargs.pop('user_timezone'))
        super().__init__(*args, **kwargs)
        
        last_auction = "the last auction I created"
        last_auction_tooltip = "You haven't created any auctions on this site yet. Once you have, you can easily reuse rules!"
        last_auction_state = "disabled" # class of the copy my last auction button
        self.auction = None
        if self.cloned_from:
            # did this user ACTUALLY create this auction, or are they stealing rules from someone else?
            self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=self.cloned_from).first()
            if self.auction:
                if not self.auction.permission_check(self.user):
                    self.auction = None
        if not self.auction:
            # either ?copy was not set, or the user didn't make that auction - doesn't matter
            self.auction = Auction.objects.exclude(is_deleted=True).filter(created_by=self.user).order_by('-date_end').first()
            if self.auction:
                if not self.auction.permission_check(self.user):
                    self.auction = None
        if self.auction:
            self.fields['cloned_from'].initial = str(self.auction.slug)
            last_auction = str(self.auction)
            last_auction_tooltip = "Same rules and locations, but with new dates and users."
            last_auction_state = ""

        if self.instance.pk:
            # editing existing auction
            print("wait, no, we should never get here!!!")
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'auction-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'is_online',
            'cloned_from',
            'date_start',
            'title',
            HTML("<h5>What kind of auction is this?</h5>"),
            Submit('online', 'Create online auction', css_id='auction-online', css_class='submit-button create-auction btn-success'),
            Div(
            HTML("<span class='text-muted'><ul><li>An auction where bidding ends automatically at a specified time.</li><li>Users will create an account on this site to join your auction.</li><li>Lots will be brought to one or more locations for exchange after bidding ends.</li></span>"),
            ),
            Submit('offline', value='Create in-person auction', css_id='auction-offline', css_class='submit-button btn-success'),
            Div(
            HTML("<span class='text-muted'><ul><li>You or your auctioneer will manually set the winners of lots.</li><li>Lots will be brought to a central location before bidding starts.</li></ul></span>"),
            ),
            Submit('clone', 'Copy ' + last_auction, css_id='auction-copy', css_class='submit-button btn-info ' + last_auction_state),
            Div(
            HTML("<span class='text-muted'><ul><li>"+ last_auction_tooltip + "</li></ul></span>"),
            )
        )

class AuctionEditForm(forms.ModelForm):
    """Make changes to an auction"""
    user_cut = forms.IntegerField(required=False, help_text="This plus the club cut must be 100%")
    
    class Meta:
        model = Auction
        fields = ['notes', 'lot_entry_fee','unsold_lot_fee','winning_bid_percent_to_club', 'date_start', 'date_end', 'lot_submission_start_date',\
            'lot_submission_end_date', 'sealed_bid','use_categories', 'promote_this_auction', 'max_lots_per_user', 'allow_additional_lots_as_donation',
            'email_users_when_invoices_ready', 'pre_register_lot_entry_fee_discount', 'pre_register_lot_discount_percent', 'allow_bidding_on_lots','only_approved_sellers',
            ]
        widgets = {
            'date_start': DateTimePickerInput(),
            'date_end': DateTimePickerInput(),
            'lot_submission_start_date': DateTimePickerInput(),
            'lot_submission_end_date': DateTimePickerInput(),
            'notes': forms.Textarea(),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user')
        self.cloned_from = kwargs.pop('cloned_from')
        timezone.activate(kwargs.pop('user_timezone'))
        super().__init__(*args, **kwargs)
        self.fields['notes'].widget.attrs = {'rows': 10}
        self.fields['winning_bid_percent_to_club'].label = "Club cut"
        self.fields['date_start'].label = "Bidding opens"
        self.fields['date_end'].label = "Bidding ends"
        self.fields['email_users_when_invoices_ready'].label = "Invoice notifications"
        self.fields['email_users_when_invoices_ready'].help_text = "Send an email to users when their invoice is ready or paid"

        #self.fields['notes'].help_text = "Foo"
        if self.instance.is_online:
            self.fields['lot_submission_end_date'].help_text = "This should be 1-24 hours before the end of your auction"
            self.fields['allow_bidding_on_lots'].help_text = "Leave this checked or people won't be able to bid!"
            self.fields['pre_register_lot_entry_fee_discount'].widget=forms.HiddenInput()
            self.fields['pre_register_lot_discount_percent'].widget=forms.HiddenInput()
        else:
            self.fields['allow_bidding_on_lots'].help_text = "Check to allow people to place bids on this website."
            self.fields['date_end'].help_text = "You should probably leave this blank so that you can manually set winners. This field has been indefinitely set to hidden - see https://github.com/iragm/fishauctions/issues/116"
            self.fields['date_end'].widget=forms.HiddenInput()
            self.fields['lot_submission_end_date'].help_text = 'This should probably be before bidding starts.  Admins (you) can add more lots at any time, this only restricts users.'
            self.fields['email_users_when_invoices_ready'].help_text = "Only works if you enter the user's email address when adding them to your auction"
        self.fields['user_cut'].initial = 100 - self.instance.winning_bid_percent_to_club
        if self.instance.pk:
            # editing existing auction
            pass
        else:
            # this is a new auction
            if self.cloned_from:
                try:
                    originalAuction = Auction.objects.get(slug=self.cloned_from, is_deleted=False)
                    if (originalAuction.created_by.pk == self.user.pk) or self.user.is_superuser:
                        # you can only clone your own auctions
                        cloneFields = ['title', 'notes', 'lot_entry_fee','unsold_lot_fee','winning_bid_percent_to_club', 'first_bid_payout', 'sealed_bid','promote_this_auction', 'max_lots_per_user', 'allow_additional_lots_as_donation','make_stats_public']
                        for field in cloneFields:
                            self.fields[field].initial = getattr(originalAuction, field)
                        self.fields['cloned_from'].initial = self.cloned_from
                except Exception as e:
                    pass
            #try:
            #    lastAuction = Auction.objects.filter(created_by=self.user).order_by('-date_end')[0]
            #    self.fields['notes'].initial = "These rules are unchanged from the last auction\n\n" + lastAuction.notes
            #except Exception as e:
                # no old auction
            else:
                self.fields['notes'].initial = "## General information\n\nYou should remove this line and edit this section to suit your auction.  Use the formatting here as an example.\n\n## Prohibited items\n- You cannot sell any fish or plants banned by state law.\n- You cannot sell large hardware items such as tanks.\n\n## Rules\n- All lots must be properly bagged.  No leaking bags!\n- You do not need to be a club member to buy or sell lots."
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'auction-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'notes',
            Div(
                PrependedAppendedText('unsold_lot_fee', '$', '.00',wrapper_class='col-lg-3', ),
                PrependedAppendedText('lot_entry_fee', '$', '.00',wrapper_class='col-lg-3', ),
                PrependedAppendedText('winning_bid_percent_to_club', '', '%',wrapper_class='col-lg-3', ),
                PrependedAppendedText('user_cut', '', '%',wrapper_class='col-lg-3', ),
                css_class='row',
            ),
            Div(
                PrependedAppendedText('pre_register_lot_entry_fee_discount', '$', '.00',wrapper_class='col-lg-3', ),
                PrependedAppendedText('pre_register_lot_discount_percent', '', '%',wrapper_class='col-lg-3', ),
                css_class='row',
            ),
            Div(
                Div('lot_submission_start_date',css_class='col-md-4',),
                Div('date_start',css_class='col-md-4',label="Bidding opens",),
                Div('lot_submission_end_date',css_class='col-md-4',),
                #Div(HTML("<span class='text-warning'><small>Hello</small></span>"),css_class="row"),
                Div('date_end',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('max_lots_per_user', css_class='col-md-4',),
                Div('allow_additional_lots_as_donation', css_class='col-md-4',),
                Div('only_approved_sellers',css_class='col-md-4',),
                css_class='row',
            ),
            Div(
                Div('use_categories',css_class='col-md-3',),
                Div('promote_this_auction', css_class='col-md-3',),
                Div('email_users_when_invoices_ready', css_class='col-md-3',),
                Div('allow_bidding_on_lots', css_class='col-md-3',),
                css_class='row',
            ),
            Submit('submit', 'Save', css_class='create-update-auction btn-success'),
        )

    # as it stands right now, we are not cleaning this at all
    # we are relying on the auction save receiver models.on_save_auction to clean anything up
    # def clean(self):
    #     cleaned_data = super().clean()
    #     date_end = cleaned_data.get("date_end")
    #     date_start = cleaned_data.get("date_start")
    #     lot_submission_end_date = cleaned_data.get("lot_submission_end_date")
    #     if date_end < timezone.now() + datetime.timedelta(hours=2):
    #         self.add_error('date_end', "The end date can't be in the past")
    #     if date_end < date_start:
    #         self.add_error('date_end', "The end date can't be before the start date")
    #     if lot_submission_end_date:
    #         if lot_submission_end_date > date_end:
    #             self.add_error('lot_submission_end_date', "Submission should end before the auction ends")
    #     return cleaned_data

class OldCreateAuctionForm(forms.ModelForm):
    """Create a new an auction"""
    
    cloned_from = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Auction
        fields = ['title', 'notes', 'lot_entry_fee','unsold_lot_fee','winning_bid_percent_to_club', 'date_start', 'date_end', 'lot_submission_start_date',\
            'lot_submission_end_date', 'sealed_bid','promote_this_auction', 'max_lots_per_user', 'allow_additional_lots_as_donation',]
        exclude = ['slug', 'first_bid_payout', 'watch_warning_email_sent', 'invoiced', 'created_by', 'code_to_add_lots', \
            'pickup_location', 'pickup_location_map', 'pickup_time', 'alternate_pickup_location', 'alternate_pickup_location_map',\
            'alternate_pickup_time','location', 'make_stats_public', ]
        widgets = {
            'date_start': DateTimePickerInput(),
            'date_end': DateTimePickerInput(),
            'lot_submission_start_date': DateTimePickerInput(),
            'lot_submission_end_date': DateTimePickerInput(),
            'notes': forms.Textarea(),
        }
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user')
        self.cloned_from = kwargs.pop('cloned_from')
        timezone.activate(kwargs.pop('user_timezone'))
        super().__init__(*args, **kwargs)
        self.fields['notes'].widget.attrs = {'rows': 10}
        if self.instance.pk:
            # editing existing auction
            pass
        else:
            # this is a new auction
            if self.cloned_from:
                try:
                    originalAuction = Auction.objects.get(slug=self.cloned_from, is_deleted=False)
                    if (originalAuction.created_by.pk == self.user.pk) or self.user.is_superuser:
                        # you can only clone your own auctions
                        cloneFields = ['title', 'notes', 'lot_entry_fee','unsold_lot_fee','winning_bid_percent_to_club', 'first_bid_payout', 'sealed_bid','promote_this_auction', 'max_lots_per_user', 'allow_additional_lots_as_donation','make_stats_public']
                        for field in cloneFields:
                            self.fields[field].initial = getattr(originalAuction, field)
                        self.fields['cloned_from'].initial = self.cloned_from
                except Exception as e:
                    pass
            #try:
            #    lastAuction = Auction.objects.filter(created_by=self.user).order_by('-date_end')[0]
            #    self.fields['notes'].initial = "These rules are unchanged from the last auction\n\n" + lastAuction.notes
            #except Exception as e:
                # no old auction
            else:
                self.fields['notes'].initial = "## General information\n\nYou should remove this line and edit this section to suit your auction.  Use the formatting here as an example.\n\n## Prohibited items\n- You cannot sell any fish or plants banned by state law.\n- You cannot sell large hardware items such as tanks.\n\n## Rules\n- All lots must be properly bagged.  No leaking bags!\n- You do not need to be a club member to buy or sell lots."
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'auction-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            'cloned_from',
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
                Div('lot_submission_start_date',css_class='col-md-4',),
                Div('lot_submission_end_date',css_class='col-md-4',),
                Div('date_end',css_class='col-md-4',),
                css_class='row',
            ),
            Div(
                Div('max_lots_per_user', css_class='col-md-4',),
                Div('allow_additional_lots_as_donation', css_class='col-md-4',),
                # Div('first_bid_payout',css_class='col-md-4',),
                css_class='row',
            ),
            Div(
                # Div('make_stats_public', css_class='col-md-4',),
                Div('sealed_bid', css_class='col-md-4',),
                Div('promote_this_auction', css_class='col-md-4',),
                css_class='row',
            ),
            Submit('submit', 'Save', css_class='create-update-auction btn-success'),
        )

    def clean(self):
        cleaned_data = super().clean()
        date_end = cleaned_data.get("date_end")
        date_start = cleaned_data.get("date_start")
        lot_submission_end_date = cleaned_data.get("lot_submission_end_date")
        if date_end < timezone.now() + datetime.timedelta(hours=2):
            self.add_error('date_end', "The end date can't be in the past")
        if date_end < date_start:
            self.add_error('date_end', "The end date can't be before the start date")
        if lot_submission_end_date:
            if lot_submission_end_date > date_end:
                self.add_error('lot_submission_end_date', "Submission should end before the auction ends")
        return cleaned_data


class CreateLotForm(forms.ModelForm):
    """Form for creating or updating of lots"""
    # Fields needed to add new species
    #species_search = forms.CharField(max_length=200, required = False)
    #species_search.help_text = "Search here for a latin or common name, or the name of a product"
    #create_new_species = forms.BooleanField(required = False)
    #new_species_name = forms.CharField(max_length=200, required = False, label="Common name")
    #new_species_name.help_text = "You can enter synonyms here, separate by commas"
    #new_species_scientific_name = forms.CharField(max_length=200, required = False, label="Scientific name")
    #new_species_scientific_name.help_text = "Enter the Latin name of this species"
    #new_species_category = ModelChoiceField(queryset=Category.objects.all().order_by('name'), required=False,label="Category")
    cloned_from = forms.IntegerField(required=False, widget=forms.HiddenInput())

    show_payment_pickup_info = forms.BooleanField(required = False, label="Show payment/pickup info")
    AUCTION_CHOICES=[(True,'Yes, this lot is part of a club auction'),
         (False,"No, I'm selling this lot independently")]
    part_of_auction = forms.ChoiceField(choices=AUCTION_CHOICES, widget=forms.RadioSelect, label="Put into an auction?", required = False)
    LENGTH_CHOICES=[(10,'Ends in 10 days'),
         (21,'Ends in 21 days')]
    run_duration = forms.ChoiceField(choices=LENGTH_CHOICES, widget=forms.RadioSelect, label="Posting duration", required = False)

    class Meta:
        model = Lot
        fields = ('relist_if_sold', 'relist_if_not_sold', 'lot_name', 'i_bred_this_fish','description','quantity','reserve_price','species_category',\
            'auction','donation', 'shipping_locations', 'buy_now_price', 'show_payment_pickup_info', 'promoted', 'part_of_auction',\
            'other_text', 'local_pickup', 'payment_paypal', 'payment_cash', 'payment_other', 'payment_other_method', 'payment_other_address',\
             'run_duration', 'reference_link',)
        exclude = ["user", 'image', 'image_source']
        widgets = {
            'description': forms.Textarea(),
            #'species': forms.HiddenInput(),
            #'cloned_from': forms.HiddenInput(),
            'shipping_locations':forms.CheckboxSelectMultiple(),
        }
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user')
        self.cloned_from = kwargs.pop('cloned_from')
        self.auction = kwargs.pop('auction')
        super().__init__(*args, **kwargs)
        self.fields['description'].widget.attrs = {'rows': 3}
        self.fields['species_category'].required = True
        self.fields['auction'].queryset = Auction.objects.exclude(is_deleted=True).filter(lot_submission_end_date__gte=timezone.now())\
            .filter(lot_submission_start_date__lte=timezone.now())\
            .filter(auctiontos__user=self.user, auctiontos__selling_allowed=True).order_by('date_end')
        if self.auction:
            if self.fields['auction'].queryset.filter(pk=self.auction.pk).exists():
                self.fields['auction'].queryset = Auction.objects.exclude(is_deleted=True).filter(pk=self.auction.pk)
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
            self.fields['run_duration'].initial = 21
            if self.instance.date_end:
                if (self.instance.date_end - self.instance.date_posted).days < 15:
                    self.fields['run_duration'].initial = 10
            self.fields['show_payment_pickup_info'].initial = False # this doesn't really matter, it just gets overridden by javascript anyway
            if self.instance.auction:
                self.fields['part_of_auction'].initial = "True"
            else:
                self.fields['part_of_auction'].initial = "False"
            #if self.instance.species:
            #    self.fields['species_search'].initial = self.instance.species.common_name.split(",")[0]
        else:
            if self.cloned_from:
                try:
                    cloneLot = Lot.objects.get(pk=self.cloned_from, is_deleted=False)
                    if (cloneLot.user.pk == self.user.pk) or self.user.is_superuser:
                        # you can only clone your lots
                        cloneFields = ['lot_name', 'quantity', 'species_category', 'description', 'i_bred_this_fish', 'reserve_price', 'buy_now_price',]
                        for field in cloneFields:
                            self.fields[field].initial = getattr(cloneLot, field)
                        self.fields['cloned_from'].initial = int(self.cloned_from)
                except:
                    pass
            # default to making new lots part of a club auction
            self.fields['part_of_auction'].initial = "True"
            self.fields['run_duration'].initial = 10
            try:
                # try to get the last lot shipping/payment info and use that, set show_payment_pickup_info as needed
                lastLot = Lot.objects.exclude(is_deleted=True).filter(user=self.user, auction__isnull=True).latest("date_of_last_user_edit")
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
            if self.auction:
                self.fields['auction'].initial = self.auction
            else:
                try:
                    # see if this user's last auction is still available
                    userData, created = UserData.objects.get_or_create(user=self.user)
                    lastUserAuction = userData.last_auction_used
                    if lastUserAuction.lot_submission_end_date > timezone.now():
                        self.fields['auction'].initial = lastUserAuction
                except:
                    pass
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'lot-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            # Div(
            #     'species',
            #     'create_new_species',
            #     css_class='d-none',
            # ),
            # HTML("<span id='species_selection'>"),
            # HTML("<h4>Species</h4>"),
            # HTML('<div class="btn-group" role="group" aria-label="Species Selection">\
            #     <button id="useExistingSpeciesButton" type="button" onclick="useExistingSpecies();" class="btn btn-secondary selected">Use existing species</button>\
            #     <button id="createNewSpeciesButton" type="button" onclick="createNewSpecies();" class="btn btn-secondary">Create new species</button>\
            #     <button id="skipSpeciesButton" type="button" onclick="skipSpecies();" class="btn btn-secondary mr-3">Skip choosing a species</button></div><br>\
            #     <span class="text-muted">You can search for products as well as species.  If you can\'t find your exact species/morph/collection location, create a new one.<br><br></span>'),
            # Div(
            #     Div('species_search',css_class='col-md-12',),
            #     css_class='row',
            # ),
            # Div(
            #     # Div('new_species_name',css_class='col-md-4',),
            #     # Div('new_species_scientific_name',css_class='col-md-4',),
            #     # Div('new_species_category',css_class='col-md-4',),
            #     css_class='row',
            # ),
            #HTML("</span><span id='details_selection'><h4>Details</h4><br>"),
            'cloned_from',
            Div(
                Div('part_of_auction',css_class='col-md-5',),
                Div('auction',css_class='col-md-8',),
                #HTML("<br><span class='text-danger col-xl-12'>You must select a pickup location before you can submit lots in an auction</span><br>"),
                Div('donation',css_class='col-md-4',),
                Div('run_duration',css_class='col-md-4',),
                Div('relist_if_not_sold',css_class='col-md-4',),
                Div('relist_if_sold',css_class='col-md-4',),
                Div('promoted',css_class='col-md-4',),
                Div('show_payment_pickup_info',css_class='col-md-12',),
                css_class='row',
            ),
            Div(
                Div('lot_name',css_class='col-md-12',),
                Div('species_category',css_class='col-md-12',),
                Div('reference_link',css_class='col-md-12',),
                css_class='row',
            ),
            Div(
                #Div('image',css_class='col-md-8',),
                #Div('image_source',css_class='col-md-4',),
                Div('description',css_class='col-md-12',),
                css_class='row',
            ),
            Div(
                Div('quantity',css_class='col-md-3',),
                Div('i_bred_this_fish',css_class='col-md-3',),
                
                Div('reserve_price',css_class='col-md-3',),
                Div('buy_now_price',css_class='col-md-3',),
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
        #create_new_species = cleaned_data.get("create_new_species")
        #new_species_name = cleaned_data.get("new_species_name")
        #new_species_scientific_name = cleaned_data.get("new_species_scientific_name")
        #new_species_category = cleaned_data.get("new_species_category")
        # if create_new_species:
        #     if not new_species_name:
        #         self.add_error('new_species_name', "Enter the common name of the new species to create")
        #     if not new_species_scientific_name:
        #         self.add_error('new_species_scientific_name', "Enter the scientific name of the new species to create")
        #     if not new_species_category:
        #         self.add_error("new_species_category", "Pick a category")
        
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
            cleaned_data['auction'] = None
            auction = None
            if not self.user.userdata.can_submit_standalone_lots:
                self.add_error('part_of_auction', "This feature is not enabled for your account")
            if not cleaned_data.get("shipping_locations") and not cleaned_data.get("local_pickup"):
                self.add_error('show_payment_pickup_info', "Select local pickup and/or a location to ship to")
            if not cleaned_data.get("payment_cash") and not cleaned_data.get("payment_paypal") and not cleaned_data.get("payment_other"):
                self.add_error('show_payment_pickup_info', "Select at least one payment method")
            if cleaned_data.get("payment_other") and not cleaned_data.get("payment_other_method"):
                self.add_error('payment_other_method', "Enter your payment method")
        if auction:
            auctiontos = AuctionTOS.objects.filter(user=self.user.pk, auction=auction).first()
            if not auctiontos:
                self.add_error('auction', "You need to join this auction before you can add lots")
            else:
                if not auctiontos.selling_allowed:
                    self.add_error('auction', "You don't have permission to sell lots in this auction")
            try:
                ban = UserBan.objects.get(banned_user=self.user.pk, user=auction.created_by.pk)
                self.add_error('auction', "You've been banned from selling lots in this auction")
            except:
                pass
            #thisAuction = Auction.objects.get(pk=auction)
            if not self.instance.pk: # only run this check when creating a lot, not when editing
                if auction.max_lots_per_user:
                    if auction.allow_additional_lots_as_donation:
                        numberOfLots = Lot.objects.exclude(is_deleted=True).filter(user=self.user, auction=auction, donation=False, banned=False).count()
                    else:
                        numberOfLots = Lot.objects.exclude(is_deleted=True).filter(user=self.user, auction=auction, banned=False).count()
                    if numberOfLots >= auction.max_lots_per_user:
                        if auction.allow_additional_lots_as_donation:
                            if not cleaned_data.get("donation"):
                                self.add_error('donation', f"You've already added {auction.max_lots_per_user} lots to this auction.  You can add more lots as a donation.")
                        else:
                            self.add_error('auction', f"You can't add more lots to this auction (Limit: {auction.max_lots_per_user})")
           
        # check to see if this lot exists already
        try:
            existingLot = Lot.objects.exclude(is_deleted=True).filter(user=self.user, lot_name=cleaned_data.get("lot_name"), description=cleaned_data.get("description"), active = True).exclude(pk=self.instance.pk)
            if existingLot:
                self.add_error('description', "You've already added a lot exactly like this.  If you mean to submit another lot, change something here so it's unique")
        except:
            pass
        return cleaned_data

        
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
    club_affiliation = forms.CharField(max_length=100, label='Club', required=False)
    club_affiliation.help_text = "Optional.  If you belong to a club, enter the name here."
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
        self.fields['club'].widget = HiddenInput()
        if self.instance.club:
                self.fields['club_affiliation'].initial = self.instance.club.name
        self.helper.layout = Layout(
            'club',
            Div(
                Div('first_name',css_class='col-md-6',),
                Div('last_name',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('phone_number',css_class='col-md-4',),
                Div('location',css_class='col-md-3',),
                Div('club_affiliation',css_class='col-md-5',),
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

class UserLabelPrefsForm(forms.ModelForm):
    class Meta:
        model = UserLabelPrefs
        exclude = ('user', )
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'printing-prefs'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div('preset',css_class='col-sm-8',),
                Div('empty_labels',css_class='col-sm-4',),
                css_class='row',
            ),
            Div(
            HTML(
            """<span class='text-warning'>You most likely do not need to change these settings!</span><br><br>Some combinations may not work, so if you have a problem, just leave a comment <a href="https://github.com/iragm/fishauctions/issues/122">here</a> and I'll fix it.</p>"""
            ),
            Div(
                Div('page_width',css_class='col-md-6',),
                Div('page_height',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('page_margin_top',css_class='col-lg-3',),
                Div('page_margin_bottom',css_class='col-lg-3',),
                Div('page_margin_left',css_class='col-lg-3',),
                Div('page_margin_right',css_class='col-lg-3',),
                css_class='row',
                ),
            Div(
                Div('label_width',css_class='col-lg-3',),
                Div('label_height',css_class='col-lg-3',),
                Div('label_margin_right',css_class='col-lg-3',),
                Div('label_margin_bottom',css_class='col-lg-3',),
                css_class='row',
                ),
            Div(
                Div('font_size', css_class='col-md-6',),
                Div('unit', css_class='col-md-6',),
                css_class='row',
                ),
            id='custom_form',),
            Submit('submit', 'Save', css_class='btn-success'),
        )

class ChangeUserPreferencesForm(forms.ModelForm):
    class Meta:
        model = UserData
        fields = ('email_visible', 'use_dark_theme', 'use_list_view', 'show_ads', 'email_me_about_new_auctions','email_me_about_new_auctions_distance',\
            'email_me_about_new_local_lots','local_distance', 'email_me_about_new_lots_ship_to_location', 'email_me_when_people_comment_on_my_lots',\
            'email_me_about_new_in_person_auctions', 'email_me_about_new_in_person_auctions_distance',
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
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_id = 'user-form'
        self.helper.form_class = 'form'
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div('email_visible',css_class='col-md-6',),
                Div('use_list_view',css_class='col-md-6',),
                css_class='row',
            ),
            Div(
                Div('use_dark_theme',css_class='col-md-6',),
                Div('show_ads',css_class='col-md-6',),
                css_class='row',
            ),
            HTML("<h4>Notifications</h4><br>"),
            Div(
                Div('email_me_when_people_comment_on_my_lots',css_class='col-md-6',),
                css_class='row',
            ),
            HTML("You'll get one email per week that contains an update on everything you've checked below<span class='text-muted'><small><br>And you'll only get that if you haven't visited the site in the last 6 days.</small></span><br><br>"),
            Div(
                Div('email_me_about_new_auctions',css_class='col-md-8',),
                Div('email_me_about_new_auctions_distance',css_class='col-md-4',),
                css_class='row',
            ),
            Div(
                Div('email_me_about_new_in_person_auctions',css_class='col-md-8',),
                Div('email_me_about_new_in_person_auctions_distance',css_class='col-md-4',),
                css_class='row',
            ),
            Div(
                Div('email_me_about_new_local_lots',css_class='col-md-8',),
                Div('local_distance',css_class='col-md-4',),
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





