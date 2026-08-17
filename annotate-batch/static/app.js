const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const statusBox = document.getElementById('status');
const filesBody = document.getElementById('filesBody');
const clearButton = document.getElementById('clearButton');

function bytesLabel(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function renderFiles(files) {
  if (!filesBody) return;

  filesBody.innerHTML = '';
  for (const file of files) {
    const row = document.createElement('tr');
    row.innerHTML = `
      <td>${file.filename}</td>
      <td>${file.modified}</td>
      <td>${bytesLabel(file.size_bytes)}</td>
      <td>
        <a href="/files/${encodeURIComponent(file.filename)}" target="_blank" rel="noopener">view</a>
        |
        <a href="/download/${encodeURIComponent(file.filename)}">download</a>
      </td>
    `;
    filesBody.appendChild(row);
  }
}

async function refreshFiles() {
  const response = await fetch('/api/files');
  const payload = await response.json();
  renderFiles(payload.files || []);
}

async function clearStoredImages() {
  const confirmed = window.confirm(
    'This will permanently delete all stored images and session folders in uploads/ and annotated/. Continue?'
  );
  if (!confirmed) return;

  if (statusBox) statusBox.textContent = 'Clearing stored images...';

  try {
    const response = await fetch('/api/clear', { method: 'POST' });
    const payload = await response.json();

    if (!response.ok) {
      if (statusBox) statusBox.textContent = `Clear failed: ${payload.error || response.statusText}`;
      return;
    }

    if (statusBox) {
      statusBox.textContent = `Cleared uploads items=${payload.removed_upload_items || 0}, annotated items=${payload.removed_annotated_items || 0}`;
    }
    await refreshFiles();
  } catch (err) {
    if (statusBox) statusBox.textContent = `Clear error: ${String(err)}`;
  }
}

async function uploadFiles(fileList) {
  if (!fileList || fileList.length === 0) return;

  const formData = new FormData();
  for (const file of fileList) {
    formData.append('files', file);
  }

  if (statusBox) statusBox.textContent = `Uploading ${fileList.length} file(s)...`;

  try {
    const response = await fetch('/upload', {
      method: 'POST',
      body: formData,
    });
    const payload = await response.json();

    if (!response.ok) {
      if (statusBox) statusBox.textContent = `Upload failed: ${payload.error || response.statusText}`;
      return;
    }

    const okCount = (payload.results || []).filter(r => r.status === 'ok').length;
    const failCount = (payload.results || []).filter(r => r.status === 'failed').length;
    const skipCount = (payload.results || []).filter(r => r.status === 'skipped').length;

    if (statusBox) statusBox.textContent = `Done. ok=${okCount}, failed=${failCount}, skipped=${skipCount}`;
    renderFiles(payload.files || []);
  } catch (err) {
    if (statusBox) statusBox.textContent = `Upload error: ${String(err)}`;
  }
}

if (dropzone) {
  dropzone.addEventListener('dragover', (event) => {
    event.preventDefault();
    dropzone.classList.add('dragover');
  });

  dropzone.addEventListener('dragleave', () => {
    dropzone.classList.remove('dragover');
  });

  dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    dropzone.classList.remove('dragover');
    uploadFiles(event.dataTransfer.files);
  });
}

if (fileInput) {
  fileInput.addEventListener('change', () => {
    uploadFiles(fileInput.files);
    fileInput.value = '';
  });
}

if (clearButton) {
  clearButton.addEventListener('click', clearStoredImages);
}

if (filesBody) {
  refreshFiles();
}
