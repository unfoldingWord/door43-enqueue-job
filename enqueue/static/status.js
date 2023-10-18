var $loading;

$(document).ready(() => {
  $loading = $("#loading").hide();
  if (
    $("#repo").val() ||
    $("#ref").val() ||
    $("#event").val() ||
    $("#job-id").val()
  ) {
    filterTable();
  }
});

var filterTableCallID;

function filterTable(repo, ref, dcs_event) {
  var queryParams = new URLSearchParams();

  var $repo = $("#repo");
  var $ref = $("#ref");
  var $event = $("#event");
  var $job_id = $("#job-id");

  if (repo) {
    $repo.val(repo);
  }
  if (ref) {
    $ref.val(ref);
  }
  if (dcs_event) {
    $event.val(dcs_event);
  }

  if ($repo.val()) {
    queryParams.set("repo", $repo.val());
  }
  if ($ref.val()) {
    queryParams.set("ref", $ref.val());
  }
  if ($event.val()) {
    queryParams.set("event", $event.val());
  }
  if ($job_id.val()) {
    queryParams.set("job_id", $job_id.val());
  }
  history.replaceState(null, null, "?" + queryParams.toString());

  var searchCriteria = {
    repo: $repo.val(),
    ref: $ref.val(),
    event: $event.val(),
    job_id: $job_id.val(),
  };
  console.log(searchCriteria);

  var $statusMessage = $("#status-message");
  var $refresh = $("#refresh");

  $loading.show();
  $statusMessage.html("");
  clearTimeout(filterTableCallID);
  $.ajax({
    type: "POST",
    url: "../get_status_table_rows",
    data: JSON.stringify(searchCriteria),
    contentType: "application/json",
    dataType: "json",
    success: function (result) {
      updateTableRows(result.table_rows);
      if ($refresh.val() && $refresh.val() != "0") {
        filterTableCallID = setTimeout(filterTable, $refresh.val() * 1000);
      }
    },
    error: function (result) {
      $statusMessage.html("ERROR OCCURRED!");
      console.log(result);
    },
    complete: function (result) {
      $loading.hide();
    },
  });
}

function updateTableRows(table_rows) {
  var $statusTable = $("#status-table");
  Object.keys(table_rows).forEach(registry => {
    console.log(registry);
    var $headerRow = $("#"+registry+"HeaderRow");
    var $toggle = $("#"+registry+"Toggle");
    var expanded = $toggle.attr("aria-expanded") == "true";
    console.log($headerRow.attr("aria-expanded"));
    var $lastRow = $headerRow;
    $("."+registry+"Row").remove();
    if (table_rows[registry].rows.length) {
      table_rows[registry].rows.forEach(row => {
        var $row = $(row);
        $row.insertAfter($lastRow);
        $row.addClass("collapse");
        $lastRow = $row;
        if (!expanded) {
          $row.attr("aria-expanded", "false");
        } else {
          $row.addClass("in");
          $row.addClass("show");
          $row.attr("aria-expanded", "true")
        }
      })
    }
    $("#"+registry+"Count").text(table_rows[registry].rows.length);
  })  
}

function queueJob() {
  var payload = $("#payload");
  var dcs_event = $("#dcs-event");
  var $statusMessage = $("#status-message");
  var submitData = {
    payload: payload.val(),
    event: dcs_event.val(),
  };
  $loading.show();
  $.ajax({
    type: "POST",
    url: "../",
    headers: {
      "X-Gitea-Event": dcs_event.val(),
    },
    data: payload.val(),
    contentType: "application/json",
    success: function (result) {
      $statusMessage.html(result.message);
      filterTable();
    },
    error: function (result) {
      $statusMessage.html("ERROR: " + result.error);
      console.log("ERROR!");
      console.log(result);
    },
    complete: function (result) {
      $loading.hide();
    },
  });
}
