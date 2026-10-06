const loadingModal = new bootstrap.Modal(document.getElementById("loadingModal"));
const appToast = new bootstrap.Toast(document.getElementById("appToast"));
const toastElement = document.getElementById("appToast");
const toastMessage = document.getElementById("toastMessage");
const cancelUploadButton = document.getElementById("cancelUpload");
const loadingMessage = document.getElementById("loadingMessage");
let currentUploadController = null;
let currentUploadJobId = null;
let currentCancelReason = null;

function createJobId() {
  if (window.crypto && window.crypto.randomUUID) {
    return window.crypto.randomUUID();
  }

  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function requestCancelUpload(reason) {
  if (!currentUploadJobId) return;

  currentCancelReason = reason;
  cancelUploadButton.disabled = true;
  cancelUploadButton.textContent = "Cancelando...";
  loadingMessage.textContent = "Solicitando cancelacion de la carga en el servidor.";

  try {
    await fetch(`/api/cargas/cancelar/${encodeURIComponent(currentUploadJobId)}`, {
      method: "POST"
    });
  } catch {
    // Si no se puede avisar al servidor, al menos se desbloquea la interfaz.
  }

  if (currentUploadController) {
    currentUploadController.abort();
  }
}

cancelUploadButton.addEventListener("click", () => {
  requestCancelUpload("Carga cancelada por el usuario.");
});

function showToast(message, success = true) {
  toastMessage.textContent = message;
  toastElement.classList.remove("text-bg-success", "text-bg-danger");
  toastElement.classList.add(success ? "text-bg-success" : "text-bg-danger");
  appToast.show();
}

function formatBytes(bytes) {
  if (!bytes) return "0 KB";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, index)).toFixed(1)} ${units[index]}`;
}

function validateFile(file) {
  const extension = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (extension !== ".xlsx") {
    throw new Error("Solo se permiten archivos Excel .xlsx.");
  }

  const maxSize = 500 * 1024 * 1024;
  if (file.size > maxSize) {
    throw new Error("El archivo supera el máximo permitido de 500 MB.");
  }
}

document.querySelectorAll(".etl-form").forEach((form) => {
  const moduleName = form.dataset.module;
  const dropZone = form.querySelector(".drop-zone");
  const fileInput = form.querySelector(".file-input");
  const selectedFile = form.querySelector(".selected-file");
  const fileName = form.querySelector(".file-name");
  const fileSize = form.querySelector(".file-size");
  const removeButton = form.querySelector(".remove-file");
  const submitButton = form.querySelector("button[type='submit']");
  const defaultSubmitHtml = submitButton.innerHTML;
  let selectedFileIsValid = false;
  let validationToken = 0;

  const resetSelectedFile = () => {
    validationToken += 1;
    selectedFileIsValid = false;
    fileInput.value = "";
    selectedFile.classList.add("d-none");
    submitButton.disabled = true;
    submitButton.innerHTML = defaultSubmitHtml;
  };

  const validateFileStructure = async (file, token) => {
    const body = new FormData();
    body.append("archivo", file);

    submitButton.disabled = true;
    submitButton.innerHTML = `<span class="spinner-border spinner-border-sm"></span> Validando archivo...`;

    try {
      const response = await fetch(`/api/validar/${moduleName}`, {
        method: "POST",
        body
      });
      const result = await response.json().catch(() => ({}));

      if (token !== validationToken) return;

      if (!response.ok) {
        throw new Error(result.detail || result.message || "El archivo no corresponde a la carga seleccionada.");
      }

      selectedFileIsValid = true;
      submitButton.disabled = false;
      submitButton.innerHTML = defaultSubmitHtml;
      showToast(result.message || "Archivo validado correctamente.");
    } catch (error) {
      if (token !== validationToken) return;
      resetSelectedFile();
      showToast(error.message, false);
    }
  };

  const selectFile = (file) => {
    try {
      validateFile(file);
      const token = ++validationToken;
      selectedFileIsValid = false;

      const transfer = new DataTransfer();
      transfer.items.add(file);
      fileInput.files = transfer.files;

      fileName.textContent = file.name;
      fileSize.textContent = formatBytes(file.size);
      selectedFile.classList.remove("d-none");

      // Vencida: la fecha de carga viene en el nombre (Contenciones ddmmyy).
      const nameDate = moduleName === "vencida"
        ? file.name.match(/contenciones\D*(\d{2})(\d{2})(\d{2})(?!\d)/i)
        : null;
      if (nameDate) {
        form.querySelector(".load-date").value = `20${nameDate[3]}-${nameDate[2]}-${nameDate[1]}`;
      }

      validateFileStructure(file, token);
    } catch (error) {
      resetSelectedFile();
      showToast(error.message, false);
    }
  };

  dropZone.addEventListener("click", () => fileInput.click());
  dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") fileInput.click();
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) selectFile(fileInput.files[0]);
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.remove("dragover");
    });
  });

  dropZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    if (file) selectFile(file);
  });

  removeButton.addEventListener("click", () => {
    resetSelectedFile();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const date = form.querySelector(".load-date").value;
    const file = fileInput.files[0];

    if (!date) {
      showToast("Debes seleccionar la fecha de carga.", false);
      return;
    }

    if (!selectedFileIsValid) {
      showToast("El archivo no ha sido validado para esta carga.", false);
      return;
    }

    if (!file) {
      showToast("Debes seleccionar un archivo Excel .xlsx.", false);
      return;
    }

    const body = new FormData();
    const jobId = createJobId();
    body.append("fecha_carga", date);
    body.append("job_id", jobId);
    body.append("archivo", file);

    document.getElementById("loadingTitle").textContent =
      `Procesando Itaú ${moduleName === "castigo" ? "Castigo" : "Vencida"}...`;
    loadingMessage.textContent = "Validando estructura del archivo y ejecutando el ETL.";

    const controller = new AbortController();
    currentUploadController = controller;
    currentUploadJobId = jobId;
    currentCancelReason = null;
    const timeoutId = window.setTimeout(() => {
      requestCancelUpload("La carga tardo demasiado y fue cancelada desde la pagina.");
    }, 5 * 60 * 1000);

    submitButton.disabled = true;
    cancelUploadButton.disabled = false;
    cancelUploadButton.textContent = "Cancelar carga";
    loadingModal.show();

    try {
      const response = await fetch(`/api/cargas/${moduleName}`, {
        method: "POST",
        body,
        signal: controller.signal
      });

      const result = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(result.detail || result.message || "No fue posible procesar el archivo.");
      }

      showToast(result.message || "Archivo procesado correctamente.");
      form.reset();
      resetSelectedFile();
      await loadHistory();
    } catch (error) {
      if (error.name === "AbortError") {
        showToast(currentCancelReason || "La carga fue cancelada.", false);
      } else {
        showToast(error.message, false);
      }
    } finally {
      window.clearTimeout(timeoutId);
      currentUploadController = null;
      currentUploadJobId = null;
      currentCancelReason = null;
      submitButton.disabled = !selectedFileIsValid;
      submitButton.innerHTML = defaultSubmitHtml;
      cancelUploadButton.disabled = false;
      cancelUploadButton.textContent = "Cancelar carga";
      loadingModal.hide();
    }
  });
});

async function loadHistory() {
  const body = document.getElementById("historyBody");

  try {
    const response = await fetch("/api/cargas");
    if (!response.ok) throw new Error();

    const rows = await response.json();

    if (!rows.length) {
      body.innerHTML = `
        <tr>
          <td colspan="5" class="text-center text-secondary py-4">
            Sin cargas registradas.
          </td>
        </tr>`;
      return;
    }

    body.innerHTML = rows.map((row) => `
      <tr>
        <td><strong>${row.modulo}</strong></td>
        <td>${row.archivo}</td>
        <td>${row.fecha_carga}</td>
        <td>
          <span class="${row.estado === "EXITOSA" ? "badge-soft-success" : row.estado === "CANCELADA" ? "badge-soft-warning" : "badge-soft-danger"}">
            ${row.estado}
          </span>
        </td>
        <td>${row.registros ?? "-"}</td>
      </tr>
    `).join("");
  } catch {
    body.innerHTML = `
      <tr>
        <td colspan="5" class="text-center text-danger py-4">
          No fue posible cargar el historial.
        </td>
      </tr>`;
  }
}

document.getElementById("refreshHistory").addEventListener("click", loadHistory);
loadHistory();
