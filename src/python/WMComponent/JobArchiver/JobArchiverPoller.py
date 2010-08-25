#!/usr/bin/env python
#pylint: disable-msg=W6501
# W6501: pass information to logging using string arguments
"""
The actual jobArchiver algorithm
"""
__all__ = []
__revision__ = "$Id: JobArchiverPoller.py,v 1.9 2010/04/15 14:50:56 mnorman Exp $"
__version__ = "$Revision: 1.9 $"

import threading
import logging
import os
import os.path
import shutil
import tarfile

from WMCore.WorkerThreads.BaseWorkerThread import BaseWorkerThread

from WMCore.JobStateMachine.ChangeState    import ChangeState

from WMCore.WMBS.Job          import Job
from WMCore.DAOFactory        import DAOFactory
from WMCore.WMBS.Fileset      import Fileset

class JobArchiverPoller(BaseWorkerThread):
    """
    Polls for Error Conditions, handles them
    """
    def __init__(self, config):
        """
        Initialise class members
        """
        BaseWorkerThread.__init__(self)
        self.config = config
        self.changeState = ChangeState(self.config)

        myThread = threading.currentThread()

        self.daoFactory = DAOFactory(package = "WMCore.WMBS",
                                     logger = myThread.logger,
                                     dbinterface = myThread.dbi)

        self.numberOfJobsToCluster = getattr(self.config.JobArchiver,
                                             "numberOfJobsToCluster", 1000)

        if not os.path.isdir(config.JobArchiver.logDir):
            if os.path.exists(config.JobArchiver.logDir):
                # Then we have some weird file in the way
                # FAIL
                raise Exception("Pre-existing file at %s" \
                                % (config.JobArchiver.logDir))
            else:
                # Create the directory
                os.makedirs(config.JobArchiver.logDir) 
    
    def setup(self, parameters):
        """
        Load DB objects required for queries
        """

        return




    def terminate(self, params):
        """
        _terminate_

        This function terminates the job after a final pass
        """
        logging.debug("terminating. doing one more pass before we die")
        self.algorithm(params)
        return


    

    def algorithm(self, parameters):
        """
	Performs the archiveJobs method, looking for each type of failure
	And deal with it as desired.
        """
        logging.debug("Running algorithm for finding finished subscriptions")
        try:
            self.archiveJobs()
            self.pollForClosable()
        except:
            raise

        return


    def archiveJobs(self):
        """
        _archiveJobs_
        
        archiveJobs will handle the master task of looking for finished jobs,
        and running the code that cleans them out.
        """
        myThread = threading.currentThread()

        doneList  = self.findFinishedJobs()

        self.cleanWorkArea(doneList)

        successList = []
        failList    = []



        for job in doneList:
            if job["outcome"] == "success":
                successList.append(job)
            else:
                failList.append(job)
                
        myThread.transaction.begin()
        self.changeState.propagate(successList, "cleanout", "success")
        self.changeState.propagate(failList, "cleanout", "exhausted")
        myThread.transaction.commit()


    def findFinishedJobs(self):
        """
        _findFinishedJobs_

        Will actually, surprisingly, find finished jobs (i.e., jobs either exhausted or successful)
        """

        jobList = []

        jobListAction = self.daoFactory(classname = "Jobs.GetAllJobs")
        jobList1  = jobListAction.execute(state = "success")
        jobList2  = jobListAction.execute(state = "exhausted")

        jobList.extend(jobList1)
        jobList.extend(jobList2)

        #logging.error("Found jobs to be finished")
        #logging.error(jobList)

        doneList = []
        
        for jobID in jobList:
            tmpJob = Job(id = jobID)
            tmpJob.load()
            doneList.append(tmpJob)


        return doneList


    def cleanWorkArea(self, doneList):
        """
        _cleanWorkArea_
        
        Upon workQueue realizing that a subscriptions is done, everything
        regarding those jobs is cleaned up.
        """

        for job in doneList:
            #print "About to clean cache for job %i" % (job['id'])
            self.cleanJobCache(job)
        
        return

    def cleanJobCache(self, job):
        """
        _cleanJobCache_

        Clears out any files still sticking around in the jobCache,
        tars up the contents and sends them off
        """

        cacheDir = job.getCache()

        if not cacheDir or not os.path.isdir(cacheDir):
            logging.error("Could not find jobCacheDir %s" % (cacheDir))
            return

        if os.listdir(cacheDir) == []:
            os.rmdir(cacheDir)
            return

        # Now we need to set up a final destination
        jobFolder = 'JobCluster_%i' \
                    % (int(job['id']/self.numberOfJobsToCluster))
        logDir = os.path.join(self.config.JobArchiver.logDir, jobFolder)
        if not os.path.exists(logDir):
            os.makedirs(logDir)

        # Otherwise we have something in there
        tarName = 'Job_%i.tar' % (job['id'])

        tarball = tarfile.open(name = os.path.join(cacheDir, tarName),
                               mode = 'w')
        for fileName in os.listdir(cacheDir):
            tarball.add(name = os.path.join(cacheDir, fileName),
                        arcname = fileName)
        tarball.close()


        shutil.move('%s/%s' % (cacheDir, tarName), \
                    '%s/%s' % (logDir, tarName))

        shutil.rmtree('%s' % (cacheDir))

        return


    def pollForClosable(self):
        """
        _pollForClosable_

        Search WMBS for filesets that can be closed and mark them as closed.
        """
        myThread = threading.currentThread()
        myThread.transaction.begin()

        closableFilesetDAO = self.daoFactory(classname = "Fileset.ListClosable")
        closableFilesets = closableFilesetDAO.execute()

        for closableFileset in closableFilesets:
            openFileset = Fileset(id = closableFileset)
            openFileset.load()

            logging.debug("Closing fileset %s" % openFileset.name)
            openFileset.markOpen(False)

        myThread.transaction.commit()

        
    
