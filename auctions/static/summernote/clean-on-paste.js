document.addEventListener('DOMContentLoaded', function () {
  // Target the Summernote editor by its container ID or class if needed
  //const summernoteEditor = document.querySelector('.note-editable');
    document.addEventListener('paste', function (event) {
          // Prevent the default paste behavior
          event.preventDefault();

          // Get the pasted content
          let pastedText = (event.clipboardData || window.clipboardData).getData('text/html');

          // Process the pasted content
          pastedText = cleanPastedContent(pastedText);

          setTimeout(() => {
            document.execCommand('insertHTML', false, pastedText);
          }, 10);
      });
});

function cleanPastedContent(content) {
  // Remove all tags except a few allowed ones (e.g., <a>, <div>, <p>, <br>, etc.)
  const allowedTags = /<(\/?)(a|div|p|br|span|em|i|li|ol|ul|strong|h[1-6]|table|tbody|thead|tr|td|abbr|b|blockquote|code|strike|u|sup|sub)[^>]*>/gi;
  const cleanedContent = content.replace(allowedTags, '$&').replace(/<\/?[^>]+(>|$)/g, "");
  return cleanedContent;
}
