// Lot images from the clipboard, and big photos made small enough to upload.
//
// The server keeps no more than 600x600 of any lot image (LotImage.image's resize_source), so a
// 25 MB phone photo is shrunk in the browser first rather than refused by nginx's request limit
// (client_max_body_size in nginx_fishauctions.conf) with a bare 413 page.
(function () {
    'use strict';

    // Comfortably under the 20M request limit, leaving room for the rest of the form.
    var MAX_UPLOAD_BYTES = 15 * 1024 * 1024;
    // Still several times what the server keeps, so nothing is lost to the shrinking.
    var MAX_SIDE = 2400;

    function tooLargeError() {
        return new Error('That image is too large to upload. Try a smaller photo or a screenshot of it.');
    }

    function shrink(file) {
        if (!window.createImageBitmap) return Promise.reject(tooLargeError());
        return createImageBitmap(file).then(function (bitmap) {
            var scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
            var canvas = document.createElement('canvas');
            canvas.width = Math.round(bitmap.width * scale);
            canvas.height = Math.round(bitmap.height * scale);
            canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
            return new Promise(function (resolve, reject) {
                canvas.toBlob(function (blob) {
                    if (!blob || blob.size > MAX_UPLOAD_BYTES) {
                        reject(tooLargeError());
                        return;
                    }
                    var name = (file.name || 'image').replace(/\.[^.]*$/, '') + '.jpg';
                    resolve(new File([blob], name, {type: 'image/jpeg'}));
                }, 'image/jpeg', 0.9);
            });
        }, function () {
            // A format this browser can't draw (HEIC in most of them).
            throw tooLargeError();
        });
    }

    // Resolves to a file small enough to send: the same file when it already is.
    function fitForUpload(file) {
        if (!file || file.size <= MAX_UPLOAD_BYTES) return Promise.resolve(file);
        return shrink(file);
    }

    // The image in a paste event, named, or null. Text pasted into a text box stays text.
    function fromPaste(event) {
        var clipboard = event.clipboardData;
        if (!clipboard) return null;
        var item = null;
        for (var i = 0; i < clipboard.items.length; i++) {
            if (clipboard.items[i].kind === 'file' && clipboard.items[i].type.indexOf('image/') === 0) {
                item = clipboard.items[i];
                break;
            }
        }
        if (!item) return null;
        var target = event.target;
        // A rich text box (the description) takes its own pastes, pictures included.
        if (target && target.isContentEditable) return null;
        var isTextEntry = target && (target.tagName === 'TEXTAREA' ||
            (target.tagName === 'INPUT' && ['file', 'checkbox', 'radio', 'submit', 'button'].indexOf(target.type) === -1));
        if (isTextEntry && clipboard.getData('text')) return null;
        var file = item.getAsFile();
        if (!file) return null;
        var ext = (file.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
        return new File([file], 'pasted-image-' + Date.now() + '.' + ext, {type: file.type});
    }

    // Put *file* in a file input, as if it had been picked, without firing a change event.
    function setInputFile(input, file) {
        var dt = new DataTransfer();
        if (file) dt.items.add(file);
        input.files = dt.files;
    }

    // Shrink whatever is picked in *input* when it is too big; *onError* gets the message otherwise.
    function guardInput(input, onError) {
        input.addEventListener('change', function () {
            var file = input.files && input.files[0];
            if (!file || file.size <= MAX_UPLOAD_BYTES) return;
            fitForUpload(file).then(function (small) {
                setInputFile(input, small);
                input.dispatchEvent(new Event('change', {bubbles: true}));
            }, function (error) {
                setInputFile(input, null);
                input.dispatchEvent(new Event('change', {bubbles: true}));
                onError(error.message);
            });
        });
    }

    window.lotImages = {
        MAX_UPLOAD_BYTES: MAX_UPLOAD_BYTES,
        fitForUpload: fitForUpload,
        fromPaste: fromPaste,
        setInputFile: setInputFile,
        guardInput: guardInput,
    };
})();
