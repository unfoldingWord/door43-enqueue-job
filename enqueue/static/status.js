var $loading;

$(document).ready(() => {
  $loading = $(".loading").hide();
  if (
    $("#repo").val() ||
    $("#ref").val() ||
    $("#event").val() ||
    $("#job-id").val()
  ) {
    filterTable();
  }
  $('[data-toggle="tooltip"]').tooltip();
});

var filterTableCallID = 0;

function filterTable(repo, ref, dcs_event) {
  var queryParams = new URLSearchParams();

  var $repo = $("#repo");
  var $ref = $("#ref");
  var $event = $("#event");
  var $job_id = $("#job-id");

  if (repo) {
    $repo.val(repo);
    $ref.val("");
    $event.val("");
    $job_id.val("");
  }
  if (ref) {
    $ref.val(ref);
    $event.val("");
    $job_id.val("");
  }
  if (dcs_event) {
    $event.val(dcs_event);
    $job_id.val("");
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

  var $statusMessage = $("#status-message");
  var $startMessage = $("#start-message");
  var $refresh = $("#refresh");
  var $statusTableWrapper = $("#status-table-wrapper");
  var $statusTable = $("#status-table");

  $loading.show();
  $startMessage.remove();
  if (filterTableCallID > 0) {
    clearTimeout(filterTableCallID);
    filterTableCallID = 0;
  }
  $.ajax({
    type: "POST",
    url: "../get_status_table_rows",
    data: JSON.stringify(searchCriteria),
    contentType: "application/json",
    dataType: "json",
    success: function (result) {
      updateTableRows(result.table_rows);
      $statusTableWrapper.show();
      if ($refresh.val() && $refresh.val() != "0" && $refresh.val() != "-1") {
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
  Object.keys(table_rows).forEach(registry => {
    var $headerRow = $("#"+registry+"HeaderRow");
    var $toggle = $("#"+registry+"Toggle");
    var expanded = $toggle.attr("aria-expanded") == "true";
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
    var count = table_rows[registry].rows.length;
    $("#"+registry+"Count").text(count+" job"+(count==0||count>1?"s":""));
  })  
}

function queueJob() {
  var payload = $("#payload");
  var dcs_event = $("#dcs-event");
  var $statusMessage = $("#status-message");
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
      $statusMessage.html("Job queued. Resulting payload: "+JSON.stringify(result));
      alert("Job queued. Filtering by the new Job ID.");
      $("#repo").val("");
      $("#ref").val("");
      $("#event").val("");
      $("#job-id").val(result.job_id);
      filterTable();
    },
    error: function (result) {
      $statusMessage.html("ERROR: " + result.error);
      console.log("ERROR!");
      console.log(result);
      alert(result);
    },
    complete: function (result) {
      $loading.hide();
    },
  });
}

function removeFinished() {
  var $statusMessage = $("#status-message");
  if (confirm("Are you sure you want to remove all finished job results older than 3 hours? This cannot be undone.") == false) {
    return;
  }

  $loading.show();
  $.ajax({
    type: "GET",
    url: "remove/finished",
    success: function (result) {
      $statusMessage.html(result.message);
      alert(result.message);
      filterTable();
    },
    error: function (result) {
      $statusMessage.html("ERROR: " + result.error);
      console.log("ERROR!");
      console.log(result);
    },
  });
}

function removeFailed() {
  if (confirm("Are you sure you want to remove all failed job results older than 24 hours? This cannot be undone.") == false) {
    return;
  }
  var $statusMessage = $("#status-message");
  $loading.show();
  $.ajax({
    type: "GET",
    url: "remove/failed",
    success: function (result) {
      $statusMessage.html(result.message);
      alert(result.message);
      filterTable();
    },
    error: function (result) {
      $statusMessage.html("ERROR: " + result.error);
      console.log("ERROR!");
      console.log(result);
    },
  });
}

function removeCanceled() {
  if (confirm("Are you sure you want to remove all canceled job results older than 3 hours? This cannot be undone.") == false) {
    return;
  }
  var $statusMessage = $("#status-message");
  $loading.show();
  $.ajax({
    type: "GET",
    url: "remove/canceled",
    success: function (result) {
      $statusMessage.html(result.message);
      alert(result.message);
      filterTable();
    },
    error: function (result) {
      $statusMessage.html("ERROR: " + result.error);
      console.log("ERROR!");
      console.log(result);
    },
  });
}
