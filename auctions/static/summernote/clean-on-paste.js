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
  // List of allowed tags
  const allowedTags = new Set([
      'a', 'div', 'p', 'br', 'span', 'em', 'i', 'li', 'ol', 'ul',
      'strong', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'tbody',
      'thead', 'tr', 'td', 'abbr', 'acronym', 'b', 'blockquote', 'code',
      'strike', 'u', 'sup', 'sub'
  ]);

  // Use DOMParser to parse the pasted HTML
  const parser = new DOMParser();
  const doc = parser.parseFromString(content, 'text/html');

  // Recursive function to remove disallowed tags
  function cleanNode(node) {
      // Iterate over child nodes
      Array.from(node.childNodes).forEach(child => {
          if (child.nodeType === Node.ELEMENT_NODE) {
              // Remove the node if it is not in the allowed tags set
              if (!allowedTags.has(child.tagName.toLowerCase())) {
                  child.remove();
              } else {
                  // Otherwise, recursively clean its children
                  cleanNode(child);
              }
          }
      });
  }

  // Clean the parsed document's body
  cleanNode(doc.body);

  // Return the cleaned HTML as a string
  return doc.body.innerHTML;
}
