if (typeof(form_id)=="undefined") {
  console.error("leave_page_warning.js should be included after defining var form_id='#some-id'");
}
var isDirty = false;
$( document ).ready(function() {
  $('.form-control').blur(function(){
    isDirty = true;
  })
});
$(form_id).submit(function(event){
    isDirty = false;
    $(this).find(':input[type=submit]').prop('disabled', true);
});
window.addEventListener('beforeunload', function(e) {
  if(isDirty) {
    e.preventDefault(); //per the standard
    e.returnValue = ''; //required for Chrome
  }
});
