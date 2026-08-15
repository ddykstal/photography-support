const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const statusBox = document.getElementById('status');
const filesBody = document.getElementById('filesBody');

function bytesLabel(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function renderFiles(files) {
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

async function uploadFiles(fileList) {
  if (!fileList || fileList.length === 0) return;

  const formData = new FormData();
  for (const file of fileList) {
    formData.append('files', file);
  }

  statusBox.textContent = `Uploading ${fileList.length} file(s)...`;

  try {
    const response = await fetch('/upload', {
      method: 'POST',
      body: formData,
    });
    const payload = await response.json();

    if (!response.ok) {
      statusBox.textContent = `Upload failed: ${payload.error || response.statusText}`;
      return;
    }

    const okCount = (payload.results || []).filter(r => r.status === 'ok').length;
    const failCount = (payload.results || []).filter(r => r.status === 'failed').length;
    const skipCount = (payload.results || []).filter(r => r.status === 'skipped').length;

    statusBox.textContent = `Done. ok=${okCount}, failed=${failCount}, skipped=${skipCount}`;
    renderFiles(payload.files || []);
  } catch (err) {
    statusBox.textContent = `Upload error: ${String(err)}`;
  }
}

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

fileInput.addEventListener('change', () => {
  uploadFiles(fileInput.files);
  fileInput.value = '';
});

refreshFiles();
