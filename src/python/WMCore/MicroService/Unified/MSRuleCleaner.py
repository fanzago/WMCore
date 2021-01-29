"""
File       : MSRuleCleaner.py

Description:
This MicroService is meant to remove Rucio rules that are no longer needed in the Workload Management system, such as:
 * block-level     rules created by WMAgent, against the origin RSE where data is getting produced
 * container-level rules created by WMAgent, with a very generic RSE expression
 * block-level     rules created by MSTransferor, for input data that is no longer in the system
 * container-level rules created by MSTransferor, for input data that is no longer in the system
In addition to that, this MicroService is now also responsible for the workflow archival, which is the final status that
workflows remain.
"""
# TODO: To Add the top level description of the logic into the module's Docstring

# futures
from __future__ import division, print_function
import json
import re
import time

# system modules
from threading import current_thread
from pprint import pformat

# WMCore modules
from WMCore.MicroService.DataStructs.DefaultStructs import RULECLEANER_REPORT
from WMCore.MicroService.DataStructs.MSRuleCleanerWflow import MSRuleCleanerWflow
from WMCore.MicroService.Unified.MSCore import MSCore
from WMCore.MicroService.Unified.Common import ckey, cert
from WMCore.Services.pycurl_manager import RequestHandler
from WMCore.Services.Rucio.Rucio import WMRucioDIDNotFoundException
from WMCore.ReqMgr.DataStructs import RequestStatus
from Utils.EmailAlert import EmailAlert
from Utils.Pipeline import Pipeline, Functor
from WMCore.WMException import WMException
from WMCore.Services.LogDB.LogDB import LogDB


class MSRuleCleanerArchivalError(WMException):
    """
    Archival Exception Class for MSRuleCleaner Module in WMCore MicroServices
    used to signal an abnormal condition if occurred during the archival step.
    """
    def __init__(self, message):
        super(MSRuleCleanerArchivalError, self).__init__(message)


class MSRuleCleanerArchivalSkip(WMException):
    """
    Archival Exception Class for MSRuleCleaner Module in WMCore MicroServices
    used to signal an expected condition which should interrupt the archival process
    """
    def __init__(self, message):
        super(MSRuleCleanerArchivalSkip, self).__init__(message)


class MSRuleCleaner(MSCore):
    """
    MSRuleCleaner.py class provides the logic used to clean the Rucio
    block level data placement rules created by WMAgent.
    """

    def __init__(self, msConfig, logger=None):
        """
        Runs the basic setup and initialization for the MSRuleCleaner module
        :param msConfig: micro service configuration
        """
        super(MSRuleCleaner, self).__init__(msConfig, logger=logger)

        self.msConfig.setdefault("verbose", True)
        self.msConfig.setdefault("interval", 60)
        self.msConfig.setdefault("services", ['ruleCleaner'])
        self.msConfig.setdefault("rucioWmaAccount", "wma_test")
        self.msConfig.setdefault("rucioMStrAccount", "wmcore_transferor")
        self.msConfig.setdefault('enableRealMode', False)

        self.mode = "RealMode" if self.msConfig['enableRealMode'] else "DryRunMode"
        self.emailAlert = EmailAlert(self.msConfig)
        self.curlMgr = RequestHandler()
        self.targetStatusRegex = re.compile(r'.*archived')
        self.logDB = LogDB(self.msConfig["logDBUrl"],
                           self.msConfig["logDBReporter"],
                           logger=self.logger)

        # Building all the Pipelines:
        pName = 'plineMSTrCont'
        self.plineMSTrCont = Pipeline(name=pName,
                                      funcLine=[Functor(self.setPlineMarker, pName),
                                                Functor(self.cleanRucioRules)])
        pName = 'plineMSTrBlock'
        self.plineMSTrBlock = Pipeline(name=pName,
                                       funcLine=[Functor(self.setPlineMarker, pName),
                                                 Functor(self.cleanRucioRules)])
        pName = 'plineAgentCont'
        self.plineAgentCont = Pipeline(name=pName,
                                       funcLine=[Functor(self.setPlineMarker, pName),
                                                 Functor(self.getRucioRules, 'container', self.msConfig['rucioWmaAccount']),
                                                 Functor(self.cleanRucioRules)])
        pName = 'plineAgentBlock'
        self.plineAgentBlock = Pipeline(name=pName,
                                        funcLine=[Functor(self.setPlineMarker, pName),
                                                  Functor(self.getRucioRules, 'block', self.msConfig['rucioWmaAccount']),
                                                  Functor(self.cleanRucioRules)])
        pName = 'plineArchive'
        self.plineArchive = Pipeline(name=pName,
                                     funcLine=[Functor(self.setPlineMarker, pName),
                                               Functor(self.findTargetStatus),
                                               Functor(self.setClean),
                                               Functor(self.setArchivalDelayExpired),
                                               Functor(self.setLogDBClean),
                                               Functor(self.archive)])

        # Building the different set of plines we will need later:
        # NOTE: The following are all the functional pipelines which are supposed to include
        #       a cleanup function and report cleanup status in the MSRuleCleanerWflow object
        self.cleanuplines = [self.plineMSTrCont,
                             self.plineMSTrBlock,
                             self.plineAgentCont,
                             self.plineAgentBlock]
        # Building an auxiliary list of cleanup pipeline names only:
        self.cleanupPipeNames = [pline.name for pline in self.cleanuplines]

        # Building lists of pipelines related only to Agents or MStransferror
        self.agentlines = [self.plineAgentCont,
                           self.plineAgentBlock]
        self.mstrlines = [self.plineMSTrCont,
                          self.plineMSTrBlock]

        # Initialization of the 'cleaned' and 'archived' counters:
        self.wfCounters = {'cleaned': {},
                           'archived': {'normalArchived': 0,
                                        'forceArchived': 0}}

    def resetCounters(self):
        """
        A simple function for zeroing the cleaned and archived counters.
        """
        for pline in self.cleanuplines:
            self.wfCounters['cleaned'][pline.name] = 0
        self.wfCounters['archived']['normalArchived'] = 0
        self.wfCounters['archived']['forceArchived'] = 0

    def execute(self, reqStatus):
        """
        Executes the whole ruleCleaner logic
        :return: summary
        """
        # start threads in MSManager which should call this method
        summary = dict(RULECLEANER_REPORT)

        self.currThread = current_thread()
        self.currThreadIdent = self.currThread.name
        self.updateReportDict(summary, "thread_id", self.currThreadIdent)
        self.resetCounters()

        self.logger.info("MSRuleCleaner is running in mode: %s.", self.mode)

        # Build the list of workflows to work on:
        try:
            requestRecords = {}
            for status in reqStatus:
                requestRecords.update(self.getRequestRecords(status))
        except Exception as err:  # general error
            msg = "Unknown exception while fetching requests from ReqMgr2. Error: %s", str(err)
            self.logger.exception(msg)
            self.updateReportDict(summary, "error", msg)

        # Call _execute() and feed the relevant pipeline with the objects popped from requestRecords
        try:
            totalNumRequests, cleanNumRequests, normalArchivedNumRequests, forceArchivedNumRequests = self._execute(requestRecords)
            msg = "\nNumber of processed workflows: %s."
            msg += "\nNumber of properly cleaned workflows: %s."
            msg += "\nNumber of normally archived workflows: %s."
            msg += "\nNumber of force archived workflows: %s."
            self.logger.info(msg,
                             totalNumRequests,
                             cleanNumRequests,
                             normalArchivedNumRequests,
                             forceArchivedNumRequests)
            self.updateReportDict(summary, "total_num_requests", totalNumRequests)
            self.updateReportDict(summary, "clean_num_requests", cleanNumRequests)
            self.updateReportDict(summary, "normal_archived_num_requests", normalArchivedNumRequests)
            self.updateReportDict(summary, "force_archived_num_requests", forceArchivedNumRequests)
        except Exception as ex:
            msg = "Unknown exception while running MSRuleCleaner thread Error: %s"
            self.logger.exception(msg, str(ex))
            self.updateReportDict(summary, "error", msg)

        return summary

    def _execute(self, reqRecords):
        """
        Executes the MSRuleCleaner pipelines based on the workflow status
        :param reqList: A list of RequestRecords to work on
        :return:        a tuple with:
                            number of properly cleaned requests
                            number of processed workflows
                            number of archived workflows
        """
        # NOTE: The Input Cleanup, the Block Level Cleanup and the Archival
        #       Pipelines are executed sequentially in the above order.
        #       This way we assure ourselves that we archive only workflows
        #       that have accomplished the needed cleanup

        cleanNumRequests = 0
        totalNumRequests = 0

        # Call the workflow dispatcher:
        for _, req in reqRecords.items():
            wflow = MSRuleCleanerWflow(req)
            self._dispatchWflow(wflow)
            msg = "\n----------------------------------------------------------"
            msg += "\nMSRuleCleanerWflow: %s"
            msg += "\n----------------------------------------------------------"
            self.logger.debug(msg, pformat(wflow))
            totalNumRequests += 1
            if self._checkClean(wflow):
                cleanNumRequests += 1

        # Report the counters:
        for pline in self.cleanuplines:
            msg = "Workflows cleaned by pipeline: %s: %d"
            self.logger.info(msg, pline.name, self.wfCounters['cleaned'][pline.name])
        normalArchivedNumRequests = self.wfCounters['archived']['normalArchived']
        forceArchivedNumRequests = self.wfCounters['archived']['forceArchived']
        self.logger.info("Workflows normally archived: %d", self.wfCounters['archived']['normalArchived'])
        self.logger.info("Workflows force archived: %d", self.wfCounters['archived']['forceArchived'])
        return totalNumRequests, cleanNumRequests, normalArchivedNumRequests, forceArchivedNumRequests

    def _dispatchWflow(self, wflow):
        """
        A function intended to dispatch a workflow (e.g based on its status)
        through one or more functional pipelines in case there is some more
        complicated logic involved in the order we execute them but not just
        a sequentially
        """
        self.logger.debug("Dispatching workflow: %s", wflow['RequestName'])
        # NOTE: The following dispatch logic is a subject to be changed at any time

        # Resolve:
        # NOTE: First resolve any preliminary flags that will be needed further
        #       in the logic of the _dispatcher() itself
        if wflow['RequestStatus'] == 'announced':
            self.getMSOutputTransferInfo(wflow)

        # Clean:
        # Do not clean any Resubmission, but still let them be archived
        if wflow['RequestType'] == 'Resubmission':
            wflow['ForceArchive'] = True
            msg = "Skipping cleanup step for workflow: %s - RequestType is %s."
            msg += " Will try to archive it directly."
            self.logger.info(msg, wflow['RequestName'], wflow['RequestType'])
        elif wflow['RequestStatus'] in ['rejected', 'aborted-completed']:
            # NOTE: We do not check the ParentageResolved flag for these
            #       workflows, but we do need to clean output data placement
            #       rules from the agents for them
            for pline in self.agentlines:
                try:
                    pline.run(wflow)
                except Exception as ex:
                    msg = "%s: General error from pipeline. Workflow: %s. Error: \n%s. "
                    msg += "\nWill retry again in the next cycle."
                    self.logger.exception(msg, pline.name, wflow['RequestName'], str(ex))
                    continue
                if wflow['CleanupStatus'][pline.name]:
                    self.wfCounters['cleaned'][pline.name] += 1
        elif wflow['RequestStatus'] == 'announced' and not wflow['ParentageResolved']:
            # NOTE: We skip workflows which are not having 'ParentageResolved'
            #       flag, but we still need some proper logging for them.
            msg = "Skipping workflow: %s - 'ParentageResolved' flag set to false."
            msg += " Will retry again in the next cycle."
            self.logger.info(msg, wflow['RequestName'])
        elif wflow['RequestStatus'] == 'announced' and not wflow['TransferDone']:
            # NOTE: We skip workflows which have not yet finalised their TransferStatus
            #       in MSOutput, but we still need some proper logging for them.
            msg = "Skipping workflow: %s - 'TransferStatus' is 'pending' or 'TransferInfo' is missing in MSOutput."
            msg += " Will retry again in the next cycle."
            self.logger.info(msg, wflow['RequestName'])
        elif wflow['RequestStatus'] == 'announced':
            for pline in self.cleanuplines:
                try:
                    pline.run(wflow)
                except Exception as ex:
                    msg = "%s: General error from pipeline. Workflow: %s. Error:  \n%s. "
                    msg += "\nWill retry again in the next cycle."
                    self.logger.exception(msg, pline.name, wflow['RequestName'], str(ex))
                    continue
                if wflow['CleanupStatus'][pline.name]:
                    self.wfCounters['cleaned'][pline.name] += 1
        else:
            # We shouldn't be here:
            msg = "Skipping workflow: %s - "
            msg += "Does not fall under any of the defined categories."
            self.logger.error(msg, wflow['RequestName'])

        # Archive:
        try:
            self.plineArchive.run(wflow)
            if wflow['ForceArchive']:
                self.wfCounters['archived']['forceArchived'] += 1
            else:
                self.wfCounters['archived']['normalArchived'] += 1
        except MSRuleCleanerArchivalSkip as ex:
            msg = "%s: Proper conditions not met: %s. "
            msg += "Skipping archival in the current cycle."
            self.logger.info(msg, wflow['PlineMarkers'][-1], ex.message())
        except MSRuleCleanerArchivalError as ex:
            msg = "%s: Archival Error: %s. "
            msg += "Will retry again in the next cycle."
            self.logger.error(msg, wflow['PlineMarkers'][-1], ex.message())
        except Exception as ex:
            msg = "%s General error from pipeline. Workflow: %s. Error: \n%s. "
            msg += "\nWill retry again in the next cycle."
            self.logger.exception(msg, wflow['PlineMarkers'][-1], wflow['RequestName'], str(ex))

    def setPlineMarker(self, wflow, pName):
        """
        A function intended to mark which is the pipeline currently working
        on the workflow. It is supposed to be called always as a first function
        in the pipeline.
        :param  wflow:   A MSRuleCleaner workflow representation
        :param  pName:   The name of the functional pipeline
        :return:         The workflow object
        """
        # NOTE: The current functional pipeline MUST always be appended at the
        #       end of the 'PlineMarkers' list

        # First get rid of the default:
        if not wflow['PlineMarkers']:
            wflow['PlineMarkers'] = []

        # Then push our current value into the markers list:
        wflow['PlineMarkers'].append(pName)

        # Populate the list of flags to be used later:
        if pName not in wflow['RulesToClean']:
            if pName in self.cleanupPipeNames:
                wflow['RulesToClean'][pName] = []
        if pName not in wflow['CleanupStatus']:
            if pName in self.cleanupPipeNames:
                wflow['CleanupStatus'][pName] = False
        return wflow

    def _checkClean(self, wflow):
        """
        An auxiliary function used to only check the temporary cleanup status.
        It basically takes the pipelines registered in 'PlineMarkers' that have
        already worked on the workflow as a mask and applies this mask over
        the set of flags in the 'CleanupStatus' field and then reduces the
        result to a single bool value
        """
        # NOTE: This is one of the few functions taking a workflow as an argument
        #       but returning a bool, since it is an auxiliary function and is not
        #       supposed to be called as a standalone function in a pipeline.
        # NOTE: `all([]) == True`, ergo all the 'rejected' && 'aborted-completed' workflows
        #       are also counted as properly cleaned and can trigger archival later

        # Build a list of bool flags based on the mask of PlineMarkers
        cleanFlagsList = [wflow['CleanupStatus'][key]
                          for key in wflow['PlineMarkers']
                          if key in wflow['CleanupStatus'].keys()]

        # If no one have worked on the workflow set the clean status to false
        if not wflow['PlineMarkers']:
            cleanStatus = False
        # If we have a mask longer than the list of flags avoid false positives
        # because of the behavior explained above - `all([]) == True`
        elif not cleanFlagsList:
            cleanStatus = False
        # Figure out the final value
        else:
            cleanStatus = all(cleanFlagsList)
        return cleanStatus

    def setClean(self, wflow):
        """
        A function to set the 'IsClean' flag based on the status from all the
        pipelines which have worked on the workflow (and have put their markers
        in the 'PlineMarkers' list)
        :param  wflow:      A MSRuleCleaner workflow representation
        :return:            The workflow object
        """
        wflow['IsClean'] = self._checkClean(wflow)
        return wflow

    def _checkLogDBClean(self, wflow):
        """
        An auxiliary function used to only check the LogDB cleanup status.
        It makes a query to LogDB in order to verify there are no any records for
        the current workflow
        :param wflow:       A MSRuleCleaner workflow representation
        :return:            True if no records were found in LogDB about wflow
        """
        cleanStatus = False
        logDBRecords = self.logDB.get(wflow['RequestName'])
        self.logger.debug("logDBRecords: %s", pformat(logDBRecords))
        if not logDBRecords:
            cleanStatus = True
        return cleanStatus

    def setLogDBClean(self, wflow):
        """
        A function to set the 'IsLogDBClean' flag based on the presence of any
        records in LogDB for the current workflow.
        :param  wflow:      A MSRuleCleaner workflow representation
        :return:            The workflow object
        """
        wflow['IsLogDBClean'] = self._checkLogDBClean(wflow)
        if not wflow['IsLogDBClean'] and wflow['IsArchivalDelayExpired']:
            wflow['IsLogDBClean'] = self._cleanLogDB(wflow)
        return wflow

    def _cleanLogDB(self, wflow):
        """
        A function to be used for cleaning all the records related to a workflow in logDB.
        :param wflow:       A MSRuleCleaner workflow representation
        :return:            True if NO errors were encountered while deleting
                            records from LogDB
        """
        cleanStatus = False
        try:
            if self.msConfig['enableRealMode']:
                self.logger.info("Deleting %s records from LogDB WMStats...", wflow['RequestName'])
                res = self.logDB.delete(wflow['RequestName'], agent=False)
                if res == 'delete-error':
                    msg = "Failed to delete logDB docs for wflow: %s" % wflow['RequestName']
                    raise MSRuleCleanerArchivalError(msg)
                cleanStatus = True
            else:
                self.logger.info("DRY-RUN: NOT Deleting %s records from LogDB WMStats...", wflow['RequestName'])
        except Exception as ex:
            msg = "General Exception while cleaning LogDB records for wflow: %s : %s"
            self.logger.exception(msg, wflow['RequestName'], str(ex))
        return cleanStatus

    def findTargetStatus(self, wflow):
        """
        Find the proper targeted archival status
        :param  wflow:      A MSRuleCleaner workflow representation
        :return:            The workflow object
        """
        # Check the available status transitions before we decide the final status
        targetStatusList = RequestStatus.REQUEST_STATE_TRANSITION.get(wflow['RequestStatus'], [])
        for status in targetStatusList:
            if self.targetStatusRegex.match(status):
                wflow['TargetStatus'] = status
        self.logger.debug("TargetStatus: %s", wflow['TargetStatus'])
        return wflow

    def _checkArchDelayExpired(self, wflow):
        """
        A function to check Archival Expiration Delay based on the information
        returned by WMStatsServer regarding the time of the last request status transition
        :param wflow:      MSRuleCleaner workflow representation
        :return:           True if the archival delay have been expired
        """
        archDelayExpired = False
        currentTime = int(time.time())
        threshold = self.msConfig['archiveDelayHours'] * 3600
        try:
            lastTransitionTime = wflow['RequestTransition'][-1]['UpdateTime']
            if lastTransitionTime and (currentTime - lastTransitionTime) > threshold:
                archDelayExpired = True
        except KeyError:
            self.logger.debug("Could not find status transition history for %s", wflow['RequestName'])
        return archDelayExpired

    def setArchivalDelayExpired(self, wflow):
        """
        A function to set the 'IsArchivalDelayExpired' flag
        """
        wflow['IsArchivalDelayExpired'] = self._checkArchDelayExpired(wflow)
        return wflow

    def archive(self, wflow):
        """
        Move the workflow to the proper archived status after checking
        the full cleanup status
        :param  wflow:      A MSRuleCleaner workflow representation
        :return:            The workflow object
        """
        # Make all the needed checks before trying to archive
        if not (wflow['IsClean'] or wflow['ForceArchive']):
            msg = "Not properly cleaned workflow: %s" % wflow['RequestName']
            raise MSRuleCleanerArchivalSkip(msg)
        if not wflow['TargetStatus']:
            msg = "Could not determine which archival status to target for: %s" % wflow['RequestName']
            raise MSRuleCleanerArchivalError(msg)
        if not wflow['IsLogDBClean']:
            msg = "LogDB records have not been cleaned for: %s" % wflow['RequestName']
            raise MSRuleCleanerArchivalSkip(msg)
        if not wflow['IsArchivalDelayExpired']:
            msg = "Archival delay period has not yet expired for: %s." % wflow['RequestName']
            raise MSRuleCleanerArchivalSkip(msg)
        if not self.msConfig['enableRealMode']:
            msg = "Real Run Mode not enabled."
            raise MSRuleCleanerArchivalSkip(msg)

        # Proceed with the actual archival:
        try:
            self.reqmgr2.updateRequestStatus(wflow['RequestName'], wflow['TargetStatus'])
        except Exception as ex:
            msg = "General Exception while trying status transition to: %s " % wflow['TargetStatus']
            msg += "for wflow: %s : %s" % (wflow['RequestName'], str(ex))
            raise MSRuleCleanerArchivalError(msg)
        return wflow

    def getMSOutputTransferInfo(self, wflow):
        """
        Fetches the transfer information from the MSOutput REST interface for
        the given workflow.
        :param  wflow:   A MSRuleCleaner workflow representation
        :return:         The workflow object
        """
        headers = {'Accept': 'application/json'}
        params = {}
        url = '%s/data/info?request=%s' % (self.msConfig['msOutputUrl'],
                                           wflow['RequestName'])
        try:
            res = self.curlMgr.getdata(url, params=params, headers=headers, ckey=ckey(), cert=cert())
            data = json.loads(res)['result'][0]
            transferInfo = data['transferDoc']
        except Exception as ex:
            msg = "General exception while fetching TransferInfo from MSOutput for %s. "
            msg += "Error: %s"
            self.logger.exception(msg, wflow['RequestName'], str(ex))
        if transferInfo is not None and transferInfo['TransferStatus'] == 'done':
            wflow['TransferDone'] = True
        return wflow

    def getRucioRules(self, wflow, gran, rucioAcct):
        """
        Queries Rucio and builds the relevant list of blocklevel rules for
        the given workflow
        :param  wflow:   A MSRuleCleaner workflow representation
        :param  gran:    Data granularity to search for Rucio rules. Possible values:
                        'block' || 'container'
        :return:         The workflow object
        """
        currPline = wflow['PlineMarkers'][-1]
        # Find all the output placement rules created by the agents
        for dataCont in wflow['OutputDatasets']:
            if gran == 'container':
                for rule in self.rucio.listDataRules(dataCont, account=rucioAcct):
                    wflow['RulesToClean'][currPline].append(rule['id'])
            elif gran == 'block':
                try:
                    blocks = self.rucio.getBlocksInContainer(dataCont)
                    for block in blocks:
                        for rule in self.rucio.listDataRules(block, account=rucioAcct):
                            wflow['RulesToClean'][currPline].append(rule['id'])
                except WMRucioDIDNotFoundException:
                    msg = "Container: %s not found in Rucio for workflow: %s."
                    self.logger.info(msg, dataCont, wflow['RequestName'])
        return wflow

    def cleanRucioRules(self, wflow):
        """
        Cleans all the Rules present in the field 'RulesToClean' in the MSRuleCleaner
        workflow representation. And fills the relevant Cleanup Status.
        :param wflow:   A MSRuleCleaner workflow representation
        :return:        The workflow object
        """
        # NOTE: The function should be called independently and sequentially from
        #       The Input and the respective BlockLevel pipelines.

        # NOTE: The current functional pipeline is always the last one in the PlineMarkers list
        currPline = wflow['PlineMarkers'][-1]
        delResults = []
        if self.msConfig['enableRealMode']:
            for rule in wflow['RulesToClean'][currPline]:
                self.logger.info("%s: Deleting ruleId: %s ", currPline, rule)
                delResult = self.rucio.deleteRule(rule)
                delResults.append(delResult)
                if not delResult:
                    self.logger.warning("%s: Failed to delete ruleId: %s ", currPline, rule)
        else:
            for rule in wflow['RulesToClean'][currPline]:
                delResults.append(True)
                self.logger.info("%s: DRY-RUN: Is about to delete ruleId: %s ", currPline, rule)

        # Set the cleanup flag:
        wflow['CleanupStatus'][currPline] = all(delResults)
        # ----------------------------------------------------------------------
        # FIXME : To be removed once the plineMSTrBlock && plineMSTrCont are
        #         developed
        if wflow['CleanupStatus'][currPline] in ['plineMSTrBlock', 'plineMSTrCont']:
            wflow['CleanupStatus'][currPline] = True
        # ----------------------------------------------------------------------
        return wflow

    def getRequestRecords(self, reqStatus):
        """
        Queries ReqMgr2 for requests in a given status.
        :param reqStatus: The status for the requests to be fetched from ReqMgr2
        :return requests: A dictionary with all the workflows in the given status
        """
        self.logger.info("Fetching requests in status: %s", reqStatus)
        result = self.reqmgr2.getRequestByStatus([reqStatus], detail=True)
        if not result:
            requests = {}
        else:
            requests = result[0]
        self.logger.info('  retrieved %s requests in status: %s', len(requests), reqStatus)
        return requests